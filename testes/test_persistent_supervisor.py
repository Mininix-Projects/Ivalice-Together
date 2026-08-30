from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import runtime_entry
import runtime_paths
import start_headless_coop as launcher


class LifetimeProcess:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class UncertainPrimaryCommitProcess:
    def __init__(self, bases: tuple[int, int, int], prepared: bytes) -> None:
        self.bases = bases
        self.memory = {
            base + launcher.GLOBAL_INSTALL_MARKER_OFFSET: prepared for base in bases
        }
        self.events: list[tuple[str, int] | tuple[str]] = []

    def read(self, address: int, size: int) -> bytes:
        return self.memory[address][:size]

    def write(self, address: int, payload: bytes) -> None:
        self.events.append(("write", address))
        self.memory[address] = bytes(payload)
        if address == self.bases[0] + launcher.GLOBAL_INSTALL_MARKER_OFFSET:
            # Model WriteProcessMemory copying every byte and then returning an
            # uncertain failure to the caller.
            raise OSError("primary write result was lost")


def test_commit_arms_residency_before_an_uncertain_primary_write():
    bases = (0x30000000, 0x31000000, 0x32000000)
    feature_bits = launcher.global_install_feature_bits(upstream_camera_v2=True)
    prepared = launcher.global_install_marker(
        state=launcher.GLOBAL_INSTALL_PREPARED,
        feature_bits=feature_bits,
    )
    committed = launcher.global_install_marker(
        state=launcher.GLOBAL_INSTALL_COMMITTED,
        feature_bits=feature_bits,
    )
    process = UncertainPrimaryCommitProcess(bases, prepared)

    def arm() -> None:
        process.events.append(("arm",))

    with pytest.raises(OSError, match="primary write result was lost"):
        launcher.commit_global_install(
            process,
            bases,
            feature_bits=feature_bits,
            before_primary_commit=arm,
        )

    primary_write = (
        "write",
        bases[0] + launcher.GLOBAL_INSTALL_MARKER_OFFSET,
    )
    assert process.events[-2:] == [("arm",), primary_write]
    assert process.memory[primary_write[1]] == committed


def test_secondary_commit_failure_never_arms_primary_residency():
    bases = (0x30000000, 0x31000000, 0x32000000)
    feature_bits = launcher.global_install_feature_bits(upstream_camera_v2=True)
    prepared = launcher.global_install_marker(
        state=launcher.GLOBAL_INSTALL_PREPARED,
        feature_bits=feature_bits,
    )
    process = UncertainPrimaryCommitProcess(bases, prepared)
    arm = mock.Mock()
    original_write = process.write

    def fail_first_secondary(address: int, payload: bytes) -> None:
        if address == bases[1] + launcher.GLOBAL_INSTALL_MARKER_OFFSET:
            raise OSError("secondary write failed")
        original_write(address, payload)

    process.write = fail_first_secondary
    with pytest.raises(OSError, match="secondary write failed"):
        launcher.commit_global_install(
            process,
            bases,
            feature_bits=feature_bits,
            before_primary_commit=arm,
        )

    arm.assert_not_called()
    assert (
        process.memory[bases[0] + launcher.GLOBAL_INSTALL_MARKER_OFFSET]
        == prepared
    )


def test_main_marks_persistent_context_before_local_teardown_latch():
    source = inspect.getsource(launcher.main)
    callback = source[source.index("def mark_global_commit_possible") :]

    assert callback.index("mark_persistent_runtime_resident(pid)") < callback.index(
        "global_install_committed = True"
    )


def test_persistent_supervisor_retries_the_same_pid_after_python_failure():
    lifetime = LifetimeProcess()
    observed_pids: list[str | None] = []

    def run_once():
        observed_pids.append(os.environ.get(runtime_paths.GAME_PID_ENV))
        if len(observed_pids) == 1:
            launcher.mark_persistent_runtime_resident(77)
            raise RuntimeError("supervisor frame failed")
        return 0

    with (
        mock.patch.dict(
            launcher.os.environ,
            {runtime_paths.GAME_PID_ENV: "123"},
            clear=False,
        ),
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77) as find_pid,
        mock.patch.object(
            launcher.coop,
            "RemoteProcess",
            return_value=lifetime,
        ) as remote_process,
        mock.patch.object(launcher, "main", side_effect=run_once) as main,
        mock.patch.object(launcher, "game_alive", return_value=True) as alive,
        mock.patch.object(launcher.time, "sleep") as sleep,
        mock.patch.object(launcher.runtime_status, "write_status") as status,
    ):
        assert launcher.run_persistent_supervisor() == 0
        assert launcher.os.environ[runtime_paths.GAME_PID_ENV] == "123"

    assert observed_pids == ["77", "77"]
    find_pid.assert_called_once_with()
    remote_process.assert_called_once_with(77)
    assert main.call_count == 2
    alive.assert_called_once_with(lifetime)
    sleep.assert_called_once_with(launcher.SUPERVISOR_RESTART_INITIAL_SECONDS)
    status.assert_called_once()
    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_persistent_supervisor_caps_backoff_but_never_exhausts_while_game_lives():
    lifetime = LifetimeProcess()
    failures = [RuntimeError(f"failure {index}") for index in range(7)]
    outcomes = iter([*failures, 0])

    def run_once():
        launcher.mark_persistent_runtime_resident(77)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with (
        mock.patch.dict(launcher.os.environ, {}, clear=True),
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(launcher, "main", side_effect=run_once) as main,
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher.time, "sleep") as sleep,
        mock.patch.object(launcher.runtime_status, "write_status"),
    ):
        assert launcher.run_persistent_supervisor() == 0
        assert runtime_paths.GAME_PID_ENV not in launcher.os.environ

    assert main.call_count == 8
    assert [call.args[0] for call in sleep.call_args_list] == [
        0.25,
        0.5,
        1.0,
        2.0,
        4.0,
        5.0,
        5.0,
    ]
    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_persistent_supervisor_stops_cleanly_after_proven_game_exit():
    lifetime = LifetimeProcess()

    def fail_after_commit():
        launcher.mark_persistent_runtime_resident(77)
        raise RuntimeError("late supervisor failure")

    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(
            launcher,
            "main",
            side_effect=fail_after_commit,
        ) as main,
        mock.patch.object(launcher, "game_alive", return_value=False),
        mock.patch.object(launcher.time, "sleep") as sleep,
        mock.patch.object(launcher.runtime_status, "write_status") as status,
    ):
        assert launcher.run_persistent_supervisor() == 0

    main.assert_called_once_with()
    sleep.assert_not_called()
    status.assert_not_called()
    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_persistent_supervisor_preserves_bootstrap_replacement_signal():
    lifetime = LifetimeProcess()
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(
            launcher,
            "main",
            side_effect=launcher.GameExitedBeforeStartup("Steam replacement"),
        ),
    ):
        with pytest.raises(launcher.GameExitedBeforeStartup):
            launcher.run_persistent_supervisor()

    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_persistent_supervisor_never_swallows_keyboard_interrupt():
    lifetime = LifetimeProcess()
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(launcher, "main", side_effect=KeyboardInterrupt),
    ):
        with pytest.raises(KeyboardInterrupt):
            launcher.run_persistent_supervisor()

    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_persistent_supervisor_exits_if_another_supervisor_wins_mutex_race():
    with (
        mock.patch.object(
            launcher,
            "acquire_mutex",
            side_effect=launcher.SupervisorAlreadyRunning("duplicate"),
        ) as acquire,
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess") as remote_process,
        mock.patch.object(launcher, "main") as main,
        mock.patch.object(launcher, "game_alive") as alive,
        mock.patch.object(launcher.time, "sleep") as sleep,
    ):
        assert launcher.run_persistent_supervisor() == 0

    acquire.assert_called_once_with(77)
    remote_process.assert_not_called()
    main.assert_not_called()
    alive.assert_not_called()
    sleep.assert_not_called()
    close_handle.assert_not_called()


def test_persistent_supervisor_propagates_a_structural_precommit_failure():
    lifetime = LifetimeProcess()
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(
            launcher,
            "main",
            side_effect=RuntimeError("PREPARED layout is structural"),
        ) as main,
        mock.patch.object(launcher, "game_alive", return_value=True) as alive,
        mock.patch.object(launcher.time, "sleep") as sleep,
        mock.patch.object(launcher.runtime_status, "write_status") as status,
    ):
        with pytest.raises(RuntimeError, match="PREPARED layout is structural"):
            launcher.run_persistent_supervisor()

    main.assert_called_once_with()
    alive.assert_called_once_with(lifetime)
    sleep.assert_not_called()
    status.assert_not_called()
    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_precommit_failure_becomes_bootstrap_exit_only_when_guard_proves_dead():
    lifetime = LifetimeProcess()
    error = RuntimeError("startup object disappeared")
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(launcher, "main", side_effect=error),
        mock.patch.object(launcher, "game_alive", return_value=False),
    ):
        with pytest.raises(launcher.GameExitedBeforeStartup) as exit_info:
            launcher.run_persistent_supervisor()

    assert exit_info.value.__cause__ is error
    close_handle.assert_called_once_with(456)
    assert lifetime.closed


def test_missing_pid_while_opening_lifetime_guard_is_bootstrap_exit():
    missing = OSError(87, "OpenProcess")
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(
            launcher.coop,
            "RemoteProcess",
            side_effect=missing,
        ) as remote_process,
        mock.patch.object(launcher.time, "sleep"),
        mock.patch.object(launcher, "main") as main,
    ):
        with pytest.raises(launcher.GameExitedBeforeStartup) as exit_info:
            launcher.run_persistent_supervisor()

    assert exit_info.value.__cause__ is missing
    assert remote_process.call_count == launcher.SUPERVISOR_LIFETIME_OPEN_ATTEMPTS
    main.assert_not_called()
    close_handle.assert_called_once_with(456)
    assert launcher._PERSISTENT_SUPERVISOR_CONTEXT is None


def test_environment_failure_releases_persistent_mutex_and_context():
    class FailingEnvironment(dict):
        def __setitem__(self, key, value):
            if key == runtime_paths.GAME_PID_ENV:
                raise OSError("environment unavailable")
            return super().__setitem__(key, value)

    environment = FailingEnvironment()
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.os, "environ", environment),
        mock.patch.object(launcher.coop, "RemoteProcess") as remote_process,
    ):
        with pytest.raises(OSError, match="environment unavailable"):
            launcher.run_persistent_supervisor()

    remote_process.assert_not_called()
    close_handle.assert_called_once_with(456)
    assert launcher._PERSISTENT_SUPERVISOR_CONTEXT is None


def test_recovery_diagnostics_can_fail_without_breaking_resident_retry():
    lifetime = LifetimeProcess()
    attempts = 0

    def run_once():
        nonlocal attempts
        attempts += 1
        launcher.mark_persistent_runtime_resident(77)
        if attempts == 1:
            raise RuntimeError("recoverable supervisor failure")
        return 0

    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle"),
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(launcher, "main", side_effect=run_once),
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher.time, "sleep") as sleep,
        mock.patch.object(
            launcher.runtime_status,
            "write_status",
            side_effect=RuntimeError("status storage unavailable"),
        ),
        mock.patch("builtins.print", side_effect=OSError("stderr unavailable")),
    ):
        assert launcher.run_persistent_supervisor() == 0

    sleep.assert_called_once_with(launcher.SUPERVISOR_RESTART_INITIAL_SECONDS)
    assert attempts == 2
    assert lifetime.closed


def test_exception_diagnostic_never_trusts_exception_stringification():
    class UnprintableError(RuntimeError):
        def __str__(self) -> str:
            raise OSError("broken __str__")

    assert launcher.exception_diagnostic(UnprintableError()) == "UnprintableError"


def test_persistent_mutex_is_held_once_and_borrowed_by_every_session():
    lifetime = LifetimeProcess()
    borrowed: list[tuple[object, bool]] = []

    def run_once():
        borrowed.append(launcher.acquire_session_mutex(77))
        return 0

    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456) as acquire,
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(launcher, "main", side_effect=run_once),
    ):
        assert launcher.run_persistent_supervisor() == 0

    assert borrowed == [(456, False)]
    acquire.assert_called_once_with(77)
    close_handle.assert_called_once_with(456)


def test_lifetime_process_open_retries_short_transient_access_failures():
    lifetime = LifetimeProcess()
    with (
        mock.patch.object(
            launcher.coop,
            "RemoteProcess",
            side_effect=[OSError("one"), OSError("two"), lifetime],
        ) as remote_process,
        mock.patch.object(launcher.time, "sleep") as sleep,
    ):
        assert launcher.open_supervisor_lifetime_process(77) is lifetime

    assert remote_process.call_count == 3
    assert sleep.call_args_list == [
        mock.call(launcher.SUPERVISOR_LIFETIME_OPEN_RETRY_SECONDS),
        mock.call(launcher.SUPERVISOR_LIFETIME_OPEN_RETRY_SECONDS),
    ]


def test_persistent_supervisor_never_swallows_system_exit():
    lifetime = LifetimeProcess()
    with (
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle"),
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=lifetime),
        mock.patch.object(launcher, "main", side_effect=SystemExit(9)),
    ):
        with pytest.raises(SystemExit) as exit_info:
            launcher.run_persistent_supervisor()

    assert exit_info.value.code == 9
    assert lifetime.closed


@pytest.mark.parametrize("arm_primary", [False, True])
def test_main_teardown_distinguishes_validation_failure_from_uncertain_commit(
    arm_primary: bool,
):
    process = SimpleNamespace(close=mock.Mock(), write=mock.Mock())
    preflight = launcher.GlobalInstallPreflight(
        "fresh",
        (0, 0, 0),
        launcher.global_install_feature_bits(upstream_camera_v2=True),
    )
    identity = SimpleNamespace()
    children = [mock.sentinel.menu, mock.sentinel.story, mock.sentinel.guard, mock.sentinel.movement]

    def fail_finalize(
        _process,
        _bases,
        *,
        feature_bits,
        before_primary_commit,
    ):
        assert feature_bits == preflight.feature_bits
        if arm_primary:
            before_primary_commit()
        raise RuntimeError("commit readback failed")

    with (
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher, "acquire_mutex", return_value=456),
        mock.patch.object(launcher.kernel32, "CloseHandle"),
        mock.patch.object(launcher.coop, "RemoteProcess", return_value=process),
        mock.patch.object(launcher, "classify_global_install", return_value=preflight),
        mock.patch.object(launcher, "reset_session_logs"),
        mock.patch.object(launcher.runtime_status, "clear_status"),
        mock.patch.object(launcher.runtime_status, "write_status"),
        mock.patch.object(
            launcher,
            "initialize_runtime_memory_and_renderer",
            return_value=(0x30000000, 0x31000000, 0x32000000, identity),
        ),
        mock.patch.object(
            launcher,
            "runtime_helper_factories",
            return_value={
                "headless_menu": mock.Mock(),
                "story_party": mock.Mock(),
                "penelo_guard": mock.Mock(),
                "movement": mock.Mock(),
            },
        ),
        mock.patch.object(launcher, "start_required_child", side_effect=children),
        mock.patch.object(launcher, "wait_child_ready"),
        mock.patch.object(launcher, "expected_movement_hook_patch", return_value=b"hook"),
        mock.patch.object(launcher, "wait_required_component"),
        mock.patch.object(launcher, "wait_gameplay", return_value=[(0, 0x1234)]),
        mock.patch.object(launcher, "sync_identity_when_stable", return_value=identity),
        mock.patch.object(launcher, "wait_runtime_ready"),
        mock.patch.object(launcher, "finalize_global_install", side_effect=fail_finalize),
        mock.patch.object(launcher, "retire_supervised_children") as retire,
    ):
        with pytest.raises(RuntimeError, match="commit readback failed"):
            launcher.main()

    retire.assert_called_once_with(
        process,
        children,
        resident_committed=arm_primary,
    )
    process.close.assert_called_once_with()


def test_main_releases_mutex_if_remote_process_constructor_fails():
    with (
        mock.patch.object(launcher.coop, "find_pid", return_value=77),
        mock.patch.object(launcher, "acquire_mutex", return_value=123),
        mock.patch.object(
            launcher,
            "open_supervisor_lifetime_process",
            side_effect=OSError("OpenProcess failed"),
        ),
        mock.patch.object(launcher.kernel32, "CloseHandle") as close_handle,
    ):
        with pytest.raises(OSError, match="OpenProcess failed"):
            launcher.main()

    close_handle.assert_called_once_with(123)


def test_autostart_prefers_the_persistent_supervisor_entrypoint():
    root = Path("C:/Portable/FF12")
    persistent = mock.Mock(return_value=0)
    one_shot = mock.Mock(side_effect=AssertionError("one-shot main used"))
    start_headless_coop = SimpleNamespace(
        GameExitedBeforeStartup=launcher.GameExitedBeforeStartup,
        run_persistent_supervisor=persistent,
        main=one_shot,
    )
    launcher_core = SimpleNamespace(
        GAME_EXECUTABLE="FFXII_TZA.exe",
        runtime_mutex_active=lambda _pid: False,
        ensure_runtime_storage=mock.Mock(),
    )
    with (
        mock.patch.object(
            runtime_entry.runtime_paths,
            "resolve_game_root",
            return_value=root,
        ),
        mock.patch.dict(
            sys.modules,
            {
                "game_process": SimpleNamespace(validate_process_id=mock.Mock()),
                "launcher_core": launcher_core,
                "start_headless_coop": start_headless_coop,
            },
        ),
    ):
        assert runtime_entry._run_autostart(4242, root / "coop") == 0

    persistent.assert_called_once_with()
    one_shot.assert_not_called()
