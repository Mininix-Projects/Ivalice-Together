from __future__ import annotations

import io
import os
from pathlib import Path
from unittest import mock

import helper_lease
import start_headless_coop as launcher


def _owned_record(tmp_path: Path, role: str = "movement"):
    child = mock.Mock(pid=12345)
    child.poll.return_value = None
    return (
        child,
        io.BytesIO(),
        io.BytesIO(),
        tmp_path / f"{role}.out.log",
        tmp_path / f"{role}.err.log",
    )


def test_helper_lease_is_exclusive_and_observable():
    game_pid = os.getpid()
    lease = helper_lease.HelperLease(game_pid, "movement")
    try:
        assert helper_lease.active(game_pid, "movement")
        with mock.patch.object(helper_lease.atexit, "register"):
            try:
                helper_lease.HelperLease(game_pid, "movement")
            except RuntimeError as error:
                assert "already running" in str(error)
            else:
                raise AssertionError("a second helper lease must be rejected")
    finally:
        lease.close()
    assert not helper_lease.active(game_pid, "movement")


def test_helper_lease_role_names_are_unique_and_keep_menu_compatibility():
    game_pid = 4242
    names = {
        role: helper_lease.mutex_name(game_pid, role)
        for role in helper_lease.ROLE_MUTEX_PREFIXES
    }

    assert set(names) == {
        "headless_menu",
        "movement",
        "story_party",
        "penelo_guard",
    }
    assert len(set(names.values())) == len(names)
    assert names["headless_menu"] == r"Local\FF12CoopMenu_4242"


def test_start_or_attach_keeps_a_live_external_owner_read_only(tmp_path: Path):
    factory = mock.Mock(side_effect=AssertionError("must not spawn a duplicate"))
    with (
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher.helper_lease, "active", return_value=True),
    ):
        record = launcher.start_or_attach_required_child(
            mock.Mock(),
            77,
            tmp_path,
            "movement",
            factory,
        )

    assert isinstance(record[0], launcher.ExternalHelper)
    assert record[0]._ff12_external
    factory.assert_not_called()


def test_live_external_owner_does_not_depend_on_a_writable_log_directory(
    tmp_path: Path,
):
    factory = mock.Mock(side_effect=AssertionError("must not spawn a duplicate"))
    with (
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher.helper_lease, "active", return_value=True),
        mock.patch.object(
            Path,
            "mkdir",
            autospec=True,
            side_effect=OSError("diagnostic volume unavailable"),
        ),
    ):
        record = launcher.start_or_attach_required_child(
            mock.Mock(),
            77,
            tmp_path / "unavailable",
            "movement",
            factory,
        )

    assert isinstance(record[0], launcher.ExternalHelper)
    assert record[3].name == "movement.out.log"
    factory.assert_not_called()


def test_start_or_attach_starts_only_an_unleased_role(tmp_path: Path):
    expected = _owned_record(tmp_path)
    factory = mock.Mock(return_value=expected)
    with (
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher.helper_lease, "active", return_value=False),
    ):
        actual = launcher.start_or_attach_required_child(
            mock.Mock(),
            77,
            tmp_path,
            "movement",
            factory,
        )

    assert actual is expected
    factory.assert_called_once_with()


def test_uncertain_lease_probe_retries_without_spawning(tmp_path: Path):
    factory = mock.Mock(side_effect=AssertionError("uncertain lease is fail-closed"))
    with (
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(
            launcher.helper_lease,
            "active",
            side_effect=[OSError("transient OpenMutex"), True],
        ),
        mock.patch.object(launcher.time, "sleep") as sleep,
    ):
        record = launcher.start_or_attach_required_child(
            mock.Mock(),
            77,
            tmp_path,
            "movement",
            factory,
        )

    assert isinstance(record[0], launcher.ExternalHelper)
    sleep.assert_called_once()
    factory.assert_not_called()


def test_external_record_is_replaced_only_after_its_lease_disappears(
    tmp_path: Path,
):
    external = launcher.external_child_record(tmp_path, 77, "movement")
    replacement = _owned_record(tmp_path)
    records = [external]
    factory = mock.Mock(return_value=replacement)

    with (
        mock.patch.object(launcher.helper_lease, "active", return_value=False),
        mock.patch.object(launcher, "game_alive", return_value=True),
    ):
        assert launcher.recover_failed_children(
            mock.Mock(),
            records,
            {"movement": factory},
            attempts=1,
        )

    assert records == [replacement]
    factory.assert_called_once_with()


def test_external_cleanup_never_signals_or_terminates_inherited_helper(
    tmp_path: Path,
):
    record = launcher.external_child_record(tmp_path, 77, "penelo_guard")
    shutdown = tmp_path / "penelo_guard.shutdown"

    launcher.cleanup_child(record)

    assert not shutdown.exists()
    assert not hasattr(record[0], "terminate")


def test_committed_supervisor_detaches_owned_helpers_while_game_is_live(
    tmp_path: Path,
):
    record = _owned_record(tmp_path)
    child, stdout, stderr, _stdout_path, _stderr_path = record

    with (
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher, "cleanup_children") as cleanup,
    ):
        launcher.retire_supervised_children(
            mock.Mock(),
            [record],
            resident_committed=True,
        )

    assert stdout.closed and stderr.closed
    child.terminate.assert_not_called()
    child.kill.assert_not_called()
    cleanup.assert_not_called()


def test_committed_detach_never_kills_on_broken_log_streams(tmp_path: Path):
    class BrokenStream:
        def __init__(self) -> None:
            self.close_attempted = False

        def close(self) -> None:
            self.close_attempted = True
            raise RuntimeError("diagnostic stream disappeared")

    child = mock.Mock(pid=12345)
    child.poll.return_value = None
    stdout = BrokenStream()
    stderr = BrokenStream()
    record = (
        child,
        stdout,
        stderr,
        tmp_path / "movement.out.log",
        tmp_path / "movement.err.log",
    )

    with (
        mock.patch.object(launcher, "game_alive", return_value=True),
        mock.patch.object(launcher, "cleanup_children") as cleanup,
    ):
        launcher.retire_supervised_children(
            mock.Mock(),
            [record],
            resident_committed=True,
        )

    assert stdout.close_attempted and stderr.close_attempted
    child.terminate.assert_not_called()
    child.kill.assert_not_called()
    cleanup.assert_not_called()


def test_precommit_failure_or_proven_game_exit_still_cleans_children(
    tmp_path: Path,
):
    for committed, alive in ((False, True), (True, False)):
        record = _owned_record(tmp_path)
        with (
            mock.patch.object(launcher, "game_alive", return_value=alive),
            mock.patch.object(launcher, "cleanup_children") as cleanup,
            mock.patch.object(launcher, "detach_children") as detach,
        ):
            launcher.retire_supervised_children(
                mock.Mock(),
                [record],
                resident_committed=committed,
            )

        cleanup.assert_called_once_with([record])
        detach.assert_not_called()


def test_committed_runtime_reconnector_assumes_all_four_roles(tmp_path: Path):
    feature_bits = launcher.global_install_feature_bits(upstream_camera_v2=True)
    preflight = launcher.GlobalInstallPreflight(
        "adopt",
        (0x30000000, 0x31000000, 0x32000000),
        feature_bits,
    )
    roles = ("headless_menu", "story_party", "penelo_guard", "movement")
    records = [launcher.external_child_record(tmp_path, 77, role) for role in roles]
    factories = {role: mock.Mock(name=f"factory_{role}") for role in roles}

    with (
        mock.patch.object(launcher, "validate_global_adoption_structure") as validate,
        mock.patch.object(launcher, "configure_committed_runtime_bases") as configure,
        mock.patch.object(
            launcher,
            "runtime_helper_factories",
            return_value=factories,
        ),
        mock.patch.object(
            launcher,
            "start_or_attach_required_child",
            side_effect=records,
        ) as assume,
        mock.patch.object(launcher, "expected_movement_hook_patch", return_value=b"x"),
        mock.patch.object(launcher, "wait_required_component") as wait,
        mock.patch.object(launcher, "game_alive", return_value=False),
        mock.patch.object(launcher, "cleanup_children") as cleanup,
        mock.patch.object(launcher.runtime_status, "write_status") as status,
        mock.patch.object(launcher.runtime_status, "clear_status") as clear,
    ):
        assert launcher.supervise_committed_runtime(
            mock.Mock(),
            77,
            preflight,
            tmp_path,
        ) == 0

    validate.assert_called_once()
    configure.assert_called_once_with(preflight.bases)
    assert [call.args[3] for call in assume.call_args_list] == list(roles)
    assert wait.call_count == 4
    status.assert_called_once()
    cleanup.assert_called_once_with(records)
    clear.assert_called_once()


def test_reconnector_reopens_fence_only_after_external_or_new_movement_is_ready(
    tmp_path: Path,
):
    feature_bits = launcher.global_install_feature_bits(upstream_camera_v2=True)
    preflight = launcher.GlobalInstallPreflight(
        "adopt",
        (0x30000000, 0x31000000, 0x32000000),
        feature_bits,
    )
    roles = ("headless_menu", "story_party", "penelo_guard", "movement")

    for external_role in ("movement", "headless_menu"):
        events = []
        records = [
            (
                launcher.external_child_record(tmp_path, 77, role)
                if role == external_role
                else _owned_record(tmp_path, role)
            )
            for role in roles
        ]
        records_by_role = dict(zip(roles, records, strict=True))
        factories = {role: mock.Mock(name=f"factory_{role}") for role in roles}
        process = mock.Mock()

        def assume(_process, _pid, _log_dir, role, _factory):
            events.append(f"attach:{role}")
            return records_by_role[role]

        def wait_component(_process, label, waiter):
            events.append(f"wait:{label}")
            if label in {
                "role admission fence close",
                "movement role consumer",
            }:
                waiter()

        def publish_fence(_process, fenced):
            events.append(f"fence:{int(bool(fenced))}")

        with (
            mock.patch.object(launcher, "validate_global_adoption_structure"),
            mock.patch.object(launcher, "configure_committed_runtime_bases"),
            mock.patch.object(
                launcher,
                "runtime_helper_factories",
                return_value=factories,
            ),
            mock.patch.object(
                launcher,
                "start_or_attach_required_child",
                side_effect=assume,
            ),
            mock.patch.object(
                launcher,
                "expected_movement_hook_patch",
                return_value=b"x",
            ),
            mock.patch.object(
                launcher,
                "movement_readiness_snapshot",
                return_value=(True, "ready"),
            ) as readiness,
            mock.patch.object(
                launcher,
                "wait_required_component",
                side_effect=wait_component,
            ),
            mock.patch.object(launcher, "supervise_children", return_value=False),
            mock.patch.object(
                launcher,
                "_publish_role_admission_fence_confirmed",
                side_effect=publish_fence,
            ),
            mock.patch.object(launcher, "game_alive", return_value=False),
            mock.patch.object(launcher, "cleanup_children"),
            mock.patch.object(launcher.runtime_status, "write_status"),
            mock.patch.object(launcher.runtime_status, "clear_status"),
        ):
            assert launcher.supervise_committed_runtime(
                process,
                77,
                preflight,
                tmp_path,
            ) == 0

        assert events.index("fence:1") < events.index("attach:headless_menu")
        assert events.index("attach:headless_menu") < events.index("attach:movement")
        assert events.index("attach:movement") < events.index(
            "wait:movement role consumer"
        )
        assert events.index("wait:movement role consumer") < events.index("fence:0")
        readiness.assert_called_once()
