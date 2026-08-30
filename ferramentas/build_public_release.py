"""Build the public core package from an explicit file allowlist."""

from __future__ import annotations

import hashlib
import json
import marshal
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
from pathlib import Path


DEVELOPMENT = Path(__file__).resolve().parents[1]
ROOT = DEVELOPMENT / "source"
DIST_EXECUTABLE = DEVELOPMENT / "artefatos/dist/IvaliceTogetherConfig.exe"
DIST_RUNTIME = DEVELOPMENT / "artefatos/dist/IvaliceTogetherRuntime"
AUTOSTART_MODULE = (
    ROOT / "ivalice-together-autostart.dll"
)
NATIVE_ROOT = DEVELOPMENT / "fontes_nativos"
NATIVE_BUILD_MANIFEST = NATIVE_ROOT / "build/native_build_manifest.json"
NATIVE_SOURCE_NAMES = (
    "p2_menu_color_dx11.cpp",
    "ivalice_together_autostart.cpp",
    "native_camera_collision_filter_v2.S",
)
NATIVE_ARTIFACT_NAMES = (
    "p2_menu_color_dx11_v85.dll",
    "ivalice-together-autostart.dll",
    "native_camera_collision_filter_v2.bin",
)
GAME_OVERRIDE_ROOT = ROOT / "game_overrides"
DAMAGE_NUMBER_LOCALES = ("us", "fr", "de", "es", "it", "kr", "cn", "ch", "in")
GAME_OVERRIDE_PATHS = (
    Path("gamedata/d3d11/artdata/menu/targetline_p_clut_2.dds.phyre"),
    Path("gamedata/d3d11/artdata/menu/targetline_p_clut_6.dds.phyre"),
    *(
        Path(
            "ps2data/image/ff12/myoshiok/"
            f"{locale}/packfiles/tex2pack_ys.bin.dir/section_000.tm2"
        )
        for locale in DAMAGE_NUMBER_LOCALES
    ),
)
PUBLIC_EXECUTABLE = Path("IvaliceTogetherConfig.exe")
DEFAULT_CONFIG_NAMES = (
    "launcher_config.json",
    "controller_config.json",
    "coop_roster_config.json",
    "session_config.json",
    "menu_theme_config.json",
)
PUBLIC_ROOT = DEVELOPMENT / "publicacao"
EMBEDDED_SOURCE_CONTRACT = (
    "controller_bindings",
    "controller_state_transport",
    "coop_launcher",
    "coop_runtime",
    "follow_handoff",
    "game_process",
    "headless_menu_shell",
    "helper_lease",
    "launcher_i18n",
    "launcher_core",
    "menu_runtime_state",
    "movement_mailbox",
    "native_action_line_theme",
    "native_target_circle_controller",
    "native_target_circle_router",
    "native_ui_tick_mux",
    "runtime_entry",
    "runtime_lifecycle",
    "runtime_paths",
    "session_state",
    "start_headless_coop",
)
EMBEDDED_ENTRY_SOURCE_CONTRACT = ("launcher_redesign",)

# ``source`` is the production import root: build tools and tests deliberately
# live elsewhere.  PyInstaller stores the Analysis entry script(s) directly in
# the outer CArchive, while every other local module belongs in PYZ.pyz.  This
# filesystem-derived rule is the complete release contract; it means adding a
# new local helper cannot silently prune it merely because a hand-maintained
# critical-module list was not updated at the same time.
#
# There are intentionally no general PYZ exclusions.  A future source file
# that is not part of the frozen runtime belongs outside ``source``.  A future
# executable entry point must be named in EMBEDDED_ENTRY_SOURCE_CONTRACT and is
# still bytecode-audited from the CArchive below.

# This tool is not part of the release payload. Add required third-party
# notices explicitly instead of copying development files into the package.
FORBIDDEN_MARKERS = (
    ".pytest_cache",
    "__pycache__",
)


def version() -> str:
    source = (ROOT / "launcher_core.py").read_text(encoding="utf-8")
    match = re.search(r'^LAUNCHER_VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if match is None:
        raise RuntimeError("launcher version was not found")
    return match.group(1)


def marker_hits(data: bytes) -> tuple[str, ...]:
    lowered = data.lower()
    hits: list[str] = []
    for marker in FORBIDDEN_MARKERS:
        if (
            marker.encode("utf-8") in lowered
            or marker.encode("utf-16le") in lowered
        ):
            hits.append(marker)
    return tuple(hits)


def production_sources() -> tuple[Path, ...]:
    """Return the source files used by the launcher and native binaries."""

    paths = [
        *ROOT.glob("*.py"),
        ROOT / "FF12CoopLauncher.spec",
        DEVELOPMENT / "fontes_nativos" / "p2_menu_color_dx11.cpp",
        DEVELOPMENT / "fontes_nativos" / "ivalice_together_autostart.cpp",
        DEVELOPMENT / "fontes_nativos" / "native_camera_collision_filter_v2.S",
    ]
    return tuple(
        sorted(
            (path for path in paths if path.is_file()),
            key=lambda path: path.name.casefold(),
        )
    )


def audit_production_sources() -> None:
    failures: list[str] = []
    for path in production_sources():
        hits = marker_hits(path.read_bytes())
        if hits:
            failures.append(
                f"{path.relative_to(DEVELOPMENT)}: {', '.join(hits)}"
            )
    if failures:
        raise RuntimeError(
            "forbidden attribution/development markers in production inputs:\n"
            + "\n".join(failures)
        )


def _stable_code_shape(value):
    """Return bytecode identity without paths or line-table metadata."""

    if isinstance(value, types.CodeType):
        return (
            "code",
            value.co_argcount,
            value.co_posonlyargcount,
            value.co_kwonlyargcount,
            value.co_nlocals,
            value.co_stacksize,
            value.co_flags,
            value.co_code,
            tuple(_stable_code_shape(item) for item in value.co_consts),
            value.co_names,
            value.co_varnames,
            value.co_freevars,
            value.co_cellvars,
            value.co_name,
        )
    if isinstance(value, tuple):
        return ("tuple", tuple(_stable_code_shape(item) for item in value))
    if isinstance(value, frozenset):
        items = (_stable_code_shape(item) for item in value)
        return ("frozenset", tuple(sorted(items, key=repr)))
    return value


def _code_digest(code: types.CodeType) -> str:
    return hashlib.sha256(marshal.dumps(_stable_code_shape(code))).hexdigest()


def expected_embedded_local_modules() -> tuple[str, ...]:
    """Return the deterministic complete set of local modules required in PYZ."""

    local_modules = {
        path.stem
        for path in ROOT.glob("*.py")
        if path.is_file()
    }
    entry_modules = set(EMBEDDED_ENTRY_SOURCE_CONTRACT)
    missing_entry_sources = entry_modules - local_modules
    if missing_entry_sources:
        missing = ", ".join(sorted(missing_entry_sources))
        raise RuntimeError(f"frozen entry-point source is missing: {missing}")
    return tuple(sorted(local_modules - entry_modules))


def verify_embedded_sources(executable: Path) -> None:
    """Reject a public runtime with missing, extra or stale local bytecode."""

    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError as error:
        raise RuntimeError("PyInstaller is required to audit the frozen runtime") from error

    archive = CArchiveReader(str(executable))
    if "PYZ.pyz" not in archive.toc:
        raise RuntimeError("frozen runtime has no PYZ.pyz archive")
    with tempfile.TemporaryDirectory(prefix="FF12 Co-op bytecode audit ") as folder:
        pyz_path = Path(folder) / "PYZ.pyz"
        pyz_path.write_bytes(archive.extract("PYZ.pyz"))
        pyz = ZlibArchiveReader(str(pyz_path))
        failures: list[str] = []
        embedded_modules = set(pyz.toc)
        source_by_module = {
            path.stem: path
            for path in ROOT.glob("*.py")
            if path.is_file()
        }
        expected_modules = set(expected_embedded_local_modules())
        critical_modules = set(EMBEDDED_SOURCE_CONTRACT)
        unknown_critical = critical_modules - expected_modules
        for module_name in sorted(unknown_critical):
            failures.append(
                f"{module_name}: critical contract has no PYZ source"
            )

        # Require the complete deterministic source set, not merely the
        # intersection that PyInstaller happened to retain.  The latter would
        # detect stale bytecode but could not detect a pruned helper at all.
        for module_name in sorted(expected_modules - embedded_modules):
            failures.append(f"{module_name}: missing from runtime")
        embedded_local_modules = embedded_modules & set(source_by_module)
        for module_name in sorted(embedded_local_modules - expected_modules):
            failures.append(
                f"{module_name}: local entry point must live in CArchive, not PYZ"
            )
        for module_name in sorted(expected_modules & embedded_modules):
            embedded = pyz.extract(module_name)
            source_path = source_by_module[module_name]
            current = compile(
                source_path.read_text(encoding="utf-8-sig"),
                source_path.name,
                "exec",
                dont_inherit=True,
                optimize=1,
            )
            if _code_digest(embedded) != _code_digest(current):
                failures.append(f"{module_name}: frozen bytecode is stale")
        for module_name in EMBEDDED_ENTRY_SOURCE_CONTRACT:
            if module_name not in archive.toc:
                failures.append(f"{module_name}: missing frozen entry point")
                continue
            try:
                embedded = marshal.loads(archive.extract(module_name))
            except (EOFError, TypeError, ValueError) as error:
                failures.append(
                    f"{module_name}: invalid frozen entry point ({error})"
                )
                continue
            source_path = ROOT / f"{module_name}.py"
            current = compile(
                source_path.read_text(encoding="utf-8-sig"),
                source_path.name,
                "exec",
                dont_inherit=True,
                optimize=1,
            )
            if _code_digest(embedded) != _code_digest(current):
                failures.append(f"{module_name}: frozen entry point is stale")
        if failures:
            raise RuntimeError("; ".join(failures))


def run_frozen_selftest(
    executable: Path,
    working_directory: Path,
    data_directory: Path | None = None,
) -> dict:
    environment = os.environ.copy()
    for variable in (
        "FF12_COOP_GAME_ROOT",
        "FF12_COOP_DATA_DIR",
        "FF12_COOP_GAME_PID",
        "FF12_COOP_CAVE_BASE",
        "FF12_COOP_DUAL_CAVE_BASE",
        "FF12_COOP_DAMAGE_CAVE_BASE",
        "FF12_COOP_RUNTIME_DIR",
    ):
        environment.pop(variable, None)
    data_directory = (
        executable.parent / "coop"
        if data_directory is None
        else Path(data_directory)
    )
    environment["FF12_COOP_GAME_ROOT"] = str(executable.parent)
    environment["FF12_COOP_DATA_DIR"] = str(data_directory)
    environment["FF12_COOP_RUNTIME_DIR"] = str(data_directory / ".runtime")
    report_path = data_directory / "runtime_selftest.json"
    report_path.unlink(missing_ok=True)
    subprocess.run(
        [str(executable), "--ff12-coop-runtime", "selftest"],
        cwd=working_directory,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        env=environment,
    )
    if not report_path.is_file():
        raise RuntimeError("frozen runtime self-test did not create its report")
    return json.loads(report_path.read_text(encoding="utf-8"))


def validate_selftest_report(report: dict, executable: Path) -> None:
    if report.get("result") != "ok":
        raise RuntimeError("frozen runtime self-test did not report success")
    expected_configs = {"launcher", "controllers", "roster", "session", "themes"}
    if set(report.get("first_run_configs", ())) != expected_configs:
        raise RuntimeError("frozen runtime did not validate clean configuration files")
    if report.get("worker_commands") != "ok":
        raise RuntimeError("frozen runtime did not validate child commands")
    expected_helper_leases = {
        "headless_menu",
        "movement",
        "story_party",
        "penelo_guard",
    }
    if set(report.get("helper_leases", ())) != expected_helper_leases:
        raise RuntimeError("frozen runtime did not validate helper leases")
    if Path(str(report.get("executable", ""))).resolve() != executable.resolve():
        raise RuntimeError("frozen runtime escaped the executable being tested")
    renderer = Path(str(report.get("persistent_renderer", "")))
    if not renderer.is_file() or "_MEI" in str(renderer.parent):
        raise RuntimeError("frozen renderer still depends on one-file temp storage")
    if sha256(renderer) != sha256(ROOT / NATIVE_ARTIFACT_NAMES[0]):
        raise RuntimeError("frozen renderer does not match the audited native build")
    bootstrap = Path(str(report.get("persistent_cave_bootstrap", "")))
    if not bootstrap.is_file() or "_MEI" in str(bootstrap.parent):
        raise RuntimeError("frozen rel32 bootstrap still depends on one-file temp storage")
    if sha256(bootstrap) != sha256(AUTOSTART_MODULE):
        raise RuntimeError("frozen rel32 bootstrap does not match the audited native build")
    camera_filter = Path(str(report.get("camera_filter", "")))
    if not camera_filter.is_file() or "_MEI" in str(camera_filter.parent):
        raise RuntimeError("frozen camera filter still depends on one-file temp storage")
    if sha256(camera_filter) != sha256(ROOT / NATIVE_ARTIFACT_NAMES[2]):
        raise RuntimeError("frozen camera filter does not match the audited native build")
    if str(report.get("embedded_autostart_sha256", "")).upper() != sha256(
        AUTOSTART_MODULE
    ):
        raise RuntimeError("frozen and packaged autostart modules do not match")
    if report.get("elemental_variants") is not None or report.get("invoke") is not None:
        raise RuntimeError("prototype payload detected in normal co-op build")


def run_auto_resolved_selftest(
    executable: Path,
    working_directory: Path,
    expected_data_directory: Path,
) -> dict:
    environment = os.environ.copy()
    for variable in (
        "FF12_COOP_GAME_ROOT",
        "FF12_COOP_DATA_DIR",
        "FF12_COOP_GAME_PID",
        "FF12_COOP_CAVE_BASE",
        "FF12_COOP_DUAL_CAVE_BASE",
        "FF12_COOP_DAMAGE_CAVE_BASE",
        "FF12_COOP_RUNTIME_DIR",
    ):
        environment.pop(variable, None)
    environment["FF12_COOP_RUNTIME_DIR"] = str(
        expected_data_directory / ".runtime"
    )
    report_path = expected_data_directory / "runtime_selftest.json"
    report_path.unlink(missing_ok=True)
    completed = subprocess.run(
        [str(executable), "--ff12-coop-runtime", "selftest"],
        cwd=working_directory,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        env=environment,
    )
    if not report_path.is_file():
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "auto-resolved frozen runtime did not use the expected data directory"
            + (f": {detail}" if detail else "")
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_selftest_report(report, executable)
    if Path(str(report.get("data_directory", ""))).resolve() != (
        expected_data_directory.resolve()
    ):
        raise RuntimeError("auto-resolved frozen runtime published the wrong directory")
    return report


def verify_packaged_installations(
    archive: Path,
) -> None:
    with (
        tempfile.TemporaryDirectory(prefix="FF12 package audit ") as folder,
        tempfile.TemporaryDirectory(
            prefix=".FF12 MO2 game audit ",
            dir=PUBLIC_ROOT,
        ) as game_folder,
    ):
        temporary_root = Path(folder)
        manual_game = (
            temporary_root / "manual" / "FINAL FANTASY XII THE ZODIAC AGE"
        )
        manual_game.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(manual_game)
        (manual_game / "x64" / "FFXII_TZA.exe").write_bytes(b"selftest")
        run_auto_resolved_selftest(
            manual_game / "x64" / PUBLIC_EXECUTABLE,
            manual_game / "x64",
            manual_game / "x64" / "coop",
        )

        mo2_mod = temporary_root / "mo2" / "mods" / "Ivalice Together"
        mo2_mod.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(mo2_mod)
        mo2_game = Path(game_folder) / "FINAL FANTASY XII THE ZODIAC AGE"
        (mo2_game / "x64").mkdir(parents=True)
        (mo2_game / "x64" / "FFXII_TZA.exe").write_bytes(b"selftest")
        run_auto_resolved_selftest(
            mo2_mod / "x64" / PUBLIC_EXECUTABLE,
            mo2_game / "x64",
            mo2_mod / "x64" / "coop",
        )


def verify_frozen_runtime() -> None:
    if not DIST_EXECUTABLE.is_file():
        raise FileNotFoundError(
            "normal configurator build is missing; build FF12CoopLauncher.spec first"
        )
    runtime_executable = DIST_RUNTIME / "IvaliceTogetherRuntime.exe"
    if not runtime_executable.is_file():
        raise FileNotFoundError("pre-extracted co-op runtime build is missing")
    expected_runtime_entries = {"IvaliceTogetherRuntime.exe", "_internal"}
    runtime_entries = {path.name for path in DIST_RUNTIME.iterdir()}
    if runtime_entries != expected_runtime_entries or not (
        DIST_RUNTIME / "_internal"
    ).is_dir():
        unexpected = sorted(runtime_entries - expected_runtime_entries)
        missing = sorted(expected_runtime_entries - runtime_entries)
        raise RuntimeError(
            "pre-extracted runtime layout is not clean: "
            f"unexpected={unexpected} missing={missing}"
        )

    verify_embedded_sources(DIST_EXECUTABLE)
    with tempfile.TemporaryDirectory(prefix="FF12 frozen selftest ") as folder:
        report = run_frozen_selftest(
            DIST_EXECUTABLE,
            ROOT,
            Path(folder) / "coop",
        )
        validate_selftest_report(report, DIST_EXECUTABLE)

    verify_embedded_sources(runtime_executable)

    with tempfile.TemporaryDirectory(prefix="FF12 co-op validation ") as folder:
        clean_root = Path(folder) / "Ivalice clean installation"
        clean_root.mkdir()
        clean_executable = clean_root / DIST_EXECUTABLE.name
        shutil.copy2(DIST_EXECUTABLE, clean_executable)
        report = run_frozen_selftest(clean_executable, clean_root)
        validate_selftest_report(report, clean_executable)
        if Path(str(report.get("data_directory", ""))).resolve() != (
            clean_root / "coop"
        ).resolve():
            raise RuntimeError("clean launcher used state from another installation")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_native_build_provenance(
    manifest_path: Path = NATIVE_BUILD_MANIFEST,
    native_root: Path = NATIVE_ROOT,
    deployed_root: Path = ROOT,
) -> None:
    """Reject stale, partial, or manually mixed native build artifacts."""

    manifest_path = Path(manifest_path)
    try:
        report = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"native build manifest is missing or invalid: {manifest_path}"
        ) from error
    if not isinstance(report, dict) or set(report) != {
        "schema",
        "sources",
        "artifacts",
    }:
        raise RuntimeError("native build manifest has an unexpected schema")
    if report.get("schema") != 1:
        raise RuntimeError("native build manifest version is unsupported")
    source_hashes = report.get("sources")
    artifact_hashes = report.get("artifacts")
    if not isinstance(source_hashes, dict) or set(source_hashes) != set(
        NATIVE_SOURCE_NAMES
    ):
        raise RuntimeError("native build manifest source contract drifted")
    if not isinstance(artifact_hashes, dict) or set(artifact_hashes) != set(
        NATIVE_ARTIFACT_NAMES
    ):
        raise RuntimeError("native build manifest artifact contract drifted")

    failures: list[str] = []
    for name in NATIVE_SOURCE_NAMES:
        path = Path(native_root) / name
        expected = str(source_hashes[name]).upper()
        if not path.is_file() or sha256(path) != expected:
            failures.append(f"{name}: source changed after native build")
    for name in NATIVE_ARTIFACT_NAMES:
        expected = str(artifact_hashes[name]).upper()
        built = manifest_path.parent / name
        deployed = Path(deployed_root) / name
        if not built.is_file() or sha256(built) != expected:
            failures.append(f"{name}: staged native artifact is stale")
        if not deployed.is_file() or sha256(deployed) != expected:
            failures.append(f"{name}: source payload was not atomically published")
    if failures:
        raise RuntimeError("; ".join(failures))


def validate_archive_integrity(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        damaged = bundle.testzip()
    if damaged is not None:
        raise RuntimeError(f"damaged ZIP entry: {damaged}")
    if len(names) != len(set(names)):
        raise RuntimeError("ZIP contains duplicate entries")
    unsafe = [
        name
        for name in names
        if Path(name).is_absolute() or ".." in Path(name).parts
    ]
    if unsafe:
        raise RuntimeError("ZIP contains unsafe paths: " + ", ".join(unsafe))
    return set(names)


def write_default_configs(directory: Path) -> None:
    """Create canonical first-run files inside the deployed mod."""

    sys.path.insert(0, str(ROOT))
    try:
        import controller_bindings
        import coop_roster
        import launcher_core
        import menu_themes
        import session_state

        launcher_core.save_launcher_config(
            dict(launcher_core.DEFAULT_LAUNCHER_CONFIG),
            directory / "launcher_config.json",
        )
        controller_bindings.save_config(
            controller_bindings.default_config(),
            directory / "controller_config.json",
        )
        coop_roster.save(
            coop_roster.default_roster(),
            directory / "coop_roster_config.json",
        )
        (directory / "session_config.json").write_text(
            json.dumps(session_state.default_config(), indent=2) + "\n",
            encoding="utf-8",
        )
        menu_themes.save_selection(
            menu_themes.default_selection(),
            directory / "menu_theme_config.json",
        )
    finally:
        sys.path.remove(str(ROOT))


def write_release() -> Path:
    release_version = version()
    release_name = f"Ivalice_Together_{release_version}"
    archive = PUBLIC_ROOT / f"{release_name}.zip"
    resolved_public = PUBLIC_ROOT.resolve()
    if DEVELOPMENT.resolve() not in resolved_public.parents:
        raise RuntimeError("public release path escaped the development area")
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{release_name}-",
        dir=PUBLIC_ROOT,
    ) as temporary:
        temporary_root = Path(temporary)
        stage = temporary_root / release_name
        temporary_archive = temporary_root / f"{release_name}.zip"
        stage.mkdir()

        payloads = [
            (DIST_EXECUTABLE, Path("x64") / PUBLIC_EXECUTABLE),
            (
                ROOT / "README.md",
                Path("x64/Ivalice Together - README.md"),
            ),
            (
                AUTOSTART_MODULE,
                Path("x64/modules/ivalice-together-autostart.dll"),
            ),
            *(
                (
                    GAME_OVERRIDE_ROOT / relative,
                    Path("mods/deploy/ff12data") / relative,
                )
                for relative in GAME_OVERRIDE_PATHS
            ),
        ]
        for source, relative in payloads:
            if not source.is_file():
                raise FileNotFoundError(f"required public payload is missing: {source}")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        runtime_destination = stage / "x64" / DIST_RUNTIME.name
        shutil.copytree(DIST_RUNTIME, runtime_destination)
        write_default_configs(stage / "x64" / "coop")
        packaged_files = tuple(path for path in stage.rglob("*") if path.is_file())
        expected = {relative for _source, relative in payloads} | {
            Path("x64/coop") / filename for filename in DEFAULT_CONFIG_NAMES
        } | {
            Path("x64") / DIST_RUNTIME.name / path.relative_to(DIST_RUNTIME)
            for path in DIST_RUNTIME.rglob("*")
            if path.is_file()
        }
        if {path.relative_to(stage) for path in packaged_files} != expected:
            raise RuntimeError("public package allowlist changed unexpectedly")
        failures: dict[str, tuple[str, ...]] = {}
        text_suffixes = {".ini", ".json", ".md", ".txt"}
        for path in packaged_files:
            if path.suffix.casefold() not in text_suffixes:
                continue
            hits = marker_hits(path.read_bytes())
            if hits:
                failures[path.relative_to(stage).as_posix()] = hits
        if failures:
            raise RuntimeError(f"forbidden markers in public package: {failures}")

        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as bundle:
            for path in packaged_files:
                # Direct installers deploy entries relative to the game root.
                # A release-name wrapper would place x64/ and mods/ too deep.
                bundle.write(path, path.relative_to(stage))

        dependency_payloads = {
            "x64/dinput8.dll",
            "x64/ff12-trampoline.dll",
            "x64/simpleLog.dll",
            "x64/modules/00-ff12-tkmalloc.dll",
            "x64/modules/ff12-file-loader.dll",
            "x64/modules/ff12-lua-loader.dll",
        }
        archived = validate_archive_integrity(temporary_archive)
        included_dependencies = dependency_payloads & archived
        if included_dependencies:
            raise RuntimeError(
                "third-party dependency entered the package: "
                + ", ".join(sorted(included_dependencies))
            )
        if archived != {path.as_posix() for path in expected}:
            raise RuntimeError("public package allowlist changed unexpectedly")
        if {Path(name).parts[0].casefold() for name in archived} - {
            "x64",
            "mods",
        }:
            raise RuntimeError("package contains an unsupported root entry")
        verify_packaged_installations(temporary_archive)
        os.replace(temporary_archive, archive)
    return archive


def main() -> int:
    audit_production_sources()
    verify_native_build_provenance()
    verify_frozen_runtime()
    archive = write_release()
    print(f"PUBLIC_RELEASE_OK archive={archive}")
    print(f"PUBLIC_RELEASE_SHA256 {sha256(archive)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(
            f"PUBLIC_RELEASE_FAILED {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)
