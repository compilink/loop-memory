from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from scripts.loopmem import storage as storage_module
from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.storage import (
    FileLease,
    ensure_directory,
    pid_is_alive,
    read_json,
    write_json_atomic,
    write_text_atomic,
)


class PrivateStorageTests(unittest.TestCase):
    def test_ensure_directory_public_primitive_exists(self):
        self.assertTrue(callable(getattr(storage_module, "ensure_directory", None)))

    def test_storage_never_mutates_posix_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory = root / "nested"
            path = directory / "state.txt"

            with (
                mock.patch("scripts.loopmem.storage.os.chmod") as chmod,
                mock.patch("scripts.loopmem.storage.os.fchmod") as fchmod,
                mock.patch("scripts.loopmem.storage.os.chown") as chown,
                mock.patch("scripts.loopmem.storage.os.fchown") as fchown,
                mock.patch.object(Path, "chmod") as path_chmod,
            ):
                ensure_directory(directory)
                write_text_atomic(path, "ok\n")
                with FileLease(
                    root / "worker.lock",
                    "worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                ):
                    pass

            chmod.assert_not_called()
            fchmod.assert_not_called()
            chown.assert_not_called()
            fchown.assert_not_called()
            path_chmod.assert_not_called()

    def test_creation_modes_follow_system_umask(self):
        source_root = Path(__file__).resolve().parents[1]
        script = """
import json
import os
from pathlib import Path
import stat
import sys

from scripts.loopmem.storage import FileLease, ensure_directory, write_text_atomic

root = Path(sys.argv[1])
old_umask = os.umask(0o027)
try:
    expected_dir = root / "expected-dir"
    actual_dir = root / "actual-dir"
    expected_dir.mkdir()
    ensure_directory(actual_dir)

    expected_file = root / "expected.txt"
    actual_file = root / "actual.txt"
    with expected_file.open("x", encoding="utf-8") as stream:
        stream.write("expected\\n")
    write_text_atomic(actual_file, "actual\\n")

    expected_lease = root / "expected.lock"
    with expected_lease.open("x", encoding="utf-8") as stream:
        stream.write("expected\\n")
    with FileLease(
        root / "actual.lock",
        "worker",
        clock=lambda: 100.0,
        pid_alive=lambda pid: False,
    ):
        actual_lease_mode = stat.S_IMODE((root / "actual.lock").stat().st_mode)

    print(json.dumps({
        "expected_dir": stat.S_IMODE(expected_dir.stat().st_mode),
        "actual_dir": stat.S_IMODE(actual_dir.stat().st_mode),
        "expected_file": stat.S_IMODE(expected_file.stat().st_mode),
        "actual_file": stat.S_IMODE(actual_file.stat().st_mode),
        "expected_lease": stat.S_IMODE(expected_lease.stat().st_mode),
        "actual_lease": actual_lease_mode,
    }))
finally:
    os.umask(old_umask)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-c", script, temp_dir],
                cwd=source_root,
                check=True,
                capture_output=True,
                text=True,
            )

        modes = json.loads(result.stdout)
        self.assertEqual(modes["actual_dir"], modes["expected_dir"])
        self.assertEqual(modes["actual_file"], modes["expected_file"])
        self.assertEqual(modes["actual_lease"], modes["expected_lease"])

    def test_atomic_write_uses_exclusive_sibling_create(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "status.md"
            real_open = Path.open
            calls: list[tuple[Path, str]] = []

            def record_open(candidate: Path, mode: str = "r", *args, **kwargs):
                calls.append((candidate, mode))
                return real_open(candidate, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", new=record_open):
                write_text_atomic(path, "ready\n")

            exclusive = [candidate for candidate, mode in calls if mode == "x"]
            self.assertEqual(len(exclusive), 1)
            self.assertEqual(exclusive[0].parent, path.parent)
            self.assertNotEqual(exclusive[0], path)

    def test_atomic_temp_collision_preserves_foreign_sibling(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "status.md"
            collision = directory / f".{path.name}.fixed.tmp"
            collision.write_text("foreign\n", encoding="utf-8")
            fake_uuid = mock.Mock(hex="fixed")

            with mock.patch(
                "scripts.loopmem.storage.uuid.uuid4",
                return_value=fake_uuid,
            ):
                with self.assertRaises(FileExistsError):
                    write_text_atomic(path, "ready\n")

            self.assertEqual(collision.read_text(encoding="utf-8"), "foreign\n")
            self.assertFalse(path.exists())

            with FileLease(
                path,
                "retry-worker",
                clock=lambda: 101.0,
                pid_alive=lambda pid: False,
            ):
                self.assertEqual(read_json(path)["owner"], "retry-worker")

            self.assertFalse(path.exists())

    def test_atomic_publish_does_not_delete_recreated_temp_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "status.md"
            temporary = directory / f".{path.name}.fixed.tmp"
            fake_uuid = mock.Mock(hex="fixed")
            real_replace = os.replace

            def replace_then_recreate(source: Path, destination: Path) -> None:
                real_replace(source, destination)
                temporary.write_text("foreign\n", encoding="utf-8")

            with (
                mock.patch(
                    "scripts.loopmem.storage.uuid.uuid4",
                    return_value=fake_uuid,
                ),
                mock.patch(
                    "scripts.loopmem.storage.os.replace",
                    side_effect=replace_then_recreate,
                ),
            ):
                write_text_atomic(path, "ready\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "ready\n")
            self.assertEqual(temporary.read_text(encoding="utf-8"), "foreign\n")

    def test_atomic_fstat_and_stat_failure_closes_stream_and_preserves_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "status.md"
            opened_streams = []
            real_open = Path.open
            real_fstat = os.fstat
            real_stat = os.stat

            def observed_open(candidate, mode="r", *args, **kwargs):
                stream = real_open(candidate, mode, *args, **kwargs)
                if mode == "x" and Path(candidate).parent == directory:
                    opened_streams.append(stream)
                return stream

            def failing_fstat(descriptor: int):
                if any(
                    not stream.closed and descriptor == stream.fileno()
                    for stream in opened_streams
                ):
                    raise OSError("injected storage fstat failure")
                return real_fstat(descriptor)

            def failing_stat(candidate, *args, **kwargs):
                if isinstance(candidate, int) and any(
                    not stream.closed and candidate == stream.fileno()
                    for stream in opened_streams
                ):
                    raise OSError("injected storage stat failure")
                return real_stat(candidate, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", new=observed_open),
                mock.patch.object(storage_module.os, "fstat", new=failing_fstat),
                mock.patch.object(storage_module.os, "stat", new=failing_stat),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected storage fstat failure",
                ):
                    write_text_atomic(path, "ready\n")

            self.assertEqual(len(opened_streams), 1)
            self.assertTrue(opened_streams[0].closed)
            self.assertFalse(path.exists())
            self.assertEqual(len(list(directory.iterdir())), 1)
            self.assertTrue(next(directory.iterdir()).name.endswith(".tmp"))

    def test_atomic_fstat_failure_preserves_foreign_temp_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "status.md"
            temporary = directory / ".status.md.fixed.tmp"
            foreign = "foreign replacement\n"
            fake_uuid = mock.Mock(hex="fixed")
            opened_streams = []
            real_open = Path.open
            real_fstat = os.fstat
            injected = False

            def observed_open(candidate, mode="r", *args, **kwargs):
                stream = real_open(candidate, mode, *args, **kwargs)
                if Path(candidate) == temporary and mode == "x":
                    opened_streams.append(stream)
                return stream

            def replace_then_fail(descriptor: int):
                nonlocal injected
                if not injected and any(
                    not stream.closed and descriptor == stream.fileno()
                    for stream in opened_streams
                ):
                    injected = True
                    real_fstat(descriptor)
                    temporary.unlink()
                    temporary.write_text(foreign, encoding="utf-8")
                    raise OSError("injected storage fstat replacement failure")
                return real_fstat(descriptor)

            with (
                mock.patch.object(storage_module.uuid, "uuid4", return_value=fake_uuid),
                mock.patch.object(Path, "open", new=observed_open),
                mock.patch.object(storage_module.os, "fstat", new=replace_then_fail),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected storage fstat replacement failure",
                ):
                    write_text_atomic(path, "ready\n")

            self.assertEqual(len(opened_streams), 1)
            self.assertTrue(opened_streams[0].closed)
            self.assertFalse(path.exists())
            self.assertEqual(temporary.read_text(encoding="utf-8"), foreign)

    def test_text_atomic_write_has_correct_content_and_no_temp_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "state"
            path = directory / "status.md"

            write_text_atomic(path, "ready\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "ready\n")
            self.assertEqual(list(directory.iterdir()), [path])

    def test_json_atomic_write_has_correct_content_and_no_temp_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "state"
            path = directory / "state.json"
            value = {"owner": "worker", "count": 2}

            write_json_atomic(path, value)

            self.assertEqual(read_json(path), value)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_serialization_failure_cleans_temp_and_preserves_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "state.json"
            original = b'{"old": true}\n'
            path.write_bytes(original)

            with self.assertRaises(TypeError):
                write_json_atomic(path, {"invalid": object()})

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_write_failure_cleans_temp_and_preserves_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "status.md"
            original = b"old status\n"
            path.write_bytes(original)

            with mock.patch(
                "scripts.loopmem.storage.os.fsync",
                side_effect=OSError("simulated fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated fsync failure"):
                    write_text_atomic(path, "new status\n")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_directory_fsync_follows_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "status.md"
            fsync_kinds: list[str] = []
            real_fsync = os.fsync

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                fsync_kinds.append(
                    "directory" if stat.S_ISDIR(mode) else "file"
                )
                real_fsync(descriptor)

            with mock.patch(
                "scripts.loopmem.storage.os.fsync",
                side_effect=record_fsync,
            ):
                write_text_atomic(path, "durable\n")

            self.assertEqual(fsync_kinds, ["file", "directory"])

    def test_directory_fsync_failure_leaves_no_atomic_temp_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "status.md"
            path.write_text("old\n", encoding="utf-8")
            real_fsync = os.fsync

            def fail_directory_fsync(descriptor: int) -> None:
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("simulated directory fsync failure")
                real_fsync(descriptor)

            with mock.patch(
                "scripts.loopmem.storage.os.fsync",
                side_effect=fail_directory_fsync,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "simulated directory fsync failure",
                ):
                    write_text_atomic(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(directory.iterdir()), [path])

    def test_corrupt_json_raises_typed_error_without_rewriting_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            original = b'{"broken":'
            path.write_bytes(original)

            with self.assertRaises(LoopMemoryError) as context:
                read_json(path)

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(path.read_bytes(), original)

    def test_non_object_json_raises_typed_error_without_rewriting_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            original = b'["not", "an", "object"]\n'
            path.write_bytes(original)

            with self.assertRaises(LoopMemoryError) as context:
                read_json(path)

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(path.read_bytes(), original)

    def test_nonfinite_json_constants_raise_corrupt_state_and_preserve_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(constant=constant):
                    path = Path(temp_dir) / f"{constant}.json"
                    original = f'{{"value": {constant}}}\n'.encode("utf-8")
                    path.write_bytes(original)

                    with self.assertRaises(LoopMemoryError) as context:
                        read_json(path)

                    self.assertEqual(context.exception.code, "corrupt_state")
                    self.assertEqual(path.read_bytes(), original)

    def test_duplicate_json_keys_raise_corrupt_state_and_preserve_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cases = (
                ("root", b'{"key": 1, "key": 2}\n'),
                ("nested", b'{"nested": {"key": 1, "key": 2}}\n'),
            )
            for name, original in cases:
                with self.subTest(name=name):
                    path = Path(temp_dir) / f"{name}.json"
                    path.write_bytes(original)

                    with self.assertRaises(LoopMemoryError) as context:
                        read_json(path)

                    self.assertEqual(context.exception.code, "corrupt_state")
                    self.assertEqual(path.read_bytes(), original)

    def test_nonfinite_json_atomic_write_preserves_destination_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for name, value in (
                ("nan", float("nan")),
                ("infinity", float("inf")),
                ("negative-infinity", float("-inf")),
            ):
                with self.subTest(name=name):
                    path = directory / f"{name}.json"
                    original = b'{"old": true}\n'
                    path.write_bytes(original)

                    with self.assertRaises(ValueError):
                        write_json_atomic(path, {"nested": {"value": value}})

                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(set(directory.iterdir()), {path})
                    path.unlink()

    def test_pid_liveness_uses_signal_zero_semantics(self):
        with mock.patch("scripts.loopmem.storage.os.kill") as kill:
            self.assertIs(pid_is_alive(321), True)
            kill.assert_called_once_with(321, 0)

        with mock.patch(
            "scripts.loopmem.storage.os.kill", side_effect=ProcessLookupError
        ):
            self.assertIs(pid_is_alive(321), False)

        with mock.patch(
            "scripts.loopmem.storage.os.kill", side_effect=PermissionError
        ):
            self.assertIs(pid_is_alive(321), True)

        with mock.patch("scripts.loopmem.storage.os.kill") as kill:
            self.assertIs(pid_is_alive(0), False)
            kill.assert_not_called()


class FileLeaseTests(unittest.TestCase):
    def write_lease(
        self,
        path: Path,
        *,
        owner: str = "old-worker",
        pid: int = 321,
        acquired_at: float = 10.0,
        expires_at: float = 200.0,
        token: str = "old-token",
    ) -> bytes:
        value = {
            "owner": owner,
            "pid": pid,
            "acquired_at": acquired_at,
            "expires_at": expires_at,
            "token": token,
        }
        content = json.dumps(value, sort_keys=True).encode("utf-8") + b"\n"
        path.write_bytes(content)
        return content

    def assert_lease_busy(self, lease: FileLease) -> None:
        with self.assertRaises(LoopMemoryError) as context:
            with lease:
                self.fail("busy lease was acquired")
        self.assertEqual(context.exception.code, "lease_busy")

    def test_invalid_lease_creation_inputs_fail_before_opening_or_creating(self):
        cases = (
            ("empty owner", "", 120, 100.0, False),
            ("zero ttl", "worker", 0, 100.0, False),
            ("negative ttl", "worker", -1, 100.0, False),
            ("boolean ttl", "worker", True, 100.0, False),
            ("infinite ttl", "worker", float("inf"), 100.0, False),
            ("negative infinite ttl", "worker", float("-inf"), 100.0, False),
            ("nan ttl", "worker", float("nan"), 100.0, False),
            ("boolean clock", "worker", 120, True, True),
            ("infinite clock", "worker", 120, float("inf"), True),
            ("negative infinite clock", "worker", 120, float("-inf"), True),
            ("nan clock", "worker", 120, float("nan"), True),
            ("infinite expiry", "worker", 1e308, 1e308, True),
            ("non-increasing expiry", "worker", 5e-324, 1.0, True),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            for name, owner, ttl_seconds, now, opens_parent in cases:
                with self.subTest(name=name):
                    path = Path(temp_dir) / f"{name.replace(' ', '-')}.lock"
                    lease = FileLease(
                        path,
                        owner,
                        ttl_seconds=ttl_seconds,
                        clock=lambda now=now: now,
                        pid_alive=lambda pid: False,
                    )

                    with mock.patch(
                        "scripts.loopmem.storage.os.open",
                        wraps=os.open,
                    ) as open_file:
                        with self.assertRaises(LoopMemoryError) as context:
                            lease.__enter__()

                    self.assertEqual(context.exception.code, "invalid_lease")
                    expected_calls = (
                        [mock.call(path.parent, os.O_RDONLY)]
                        if opens_parent
                        else []
                    )
                    self.assertEqual(open_file.call_args_list, expected_calls)
                    self.assertFalse(path.exists())

    def test_invalid_lease_timestamp_order_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for expires_at in (100.0, 99.0):
                with self.subTest(expires_at=expires_at):
                    path = Path(temp_dir) / f"worker-{expires_at}.lock"
                    original = self.write_lease(
                        path,
                        acquired_at=100.0,
                        expires_at=expires_at,
                    )

                    with self.assertRaises(LoopMemoryError) as context:
                        with FileLease(
                            path,
                            "new-worker",
                            clock=lambda: 200.0,
                            pid_alive=lambda pid: False,
                        ):
                            self.fail("invalid stored lease was acquired")

                    self.assertEqual(context.exception.code, "corrupt_state")
                    self.assertEqual(path.read_bytes(), original)

    def test_fstat_failure_cleans_created_lease_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"

            with mock.patch(
                "scripts.loopmem.storage.os.fstat",
                side_effect=OSError("simulated fstat failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated fstat failure"):
                    FileLease(
                        path,
                        "worker",
                        clock=lambda: 100.0,
                        pid_alive=lambda pid: False,
                    ).__enter__()

            self.assertFalse(path.exists())

    def test_fstat_failure_does_not_delete_foreign_lease_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            foreign = b"foreign replacement\n"
            real_fstat = os.fstat
            injected = False

            def replace_then_fail(descriptor: int):
                nonlocal injected
                if not injected and stat.S_ISREG(real_fstat(descriptor).st_mode):
                    injected = True
                    path.unlink()
                    path.write_bytes(foreign)
                    raise OSError("simulated fstat failure after replacement")
                return real_fstat(descriptor)

            with mock.patch(
                "scripts.loopmem.storage.os.fstat",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "simulated fstat failure after replacement",
                ):
                    FileLease(
                        path,
                        "worker",
                        clock=lambda: 100.0,
                        pid_alive=lambda pid: False,
                    ).__enter__()

            self.assertEqual(path.read_bytes(), foreign)

    def test_failed_creation_cleanup_fsyncs_parent_after_unlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_directory_fsync = storage_module._ParentDirectoryFlock.fsync
            real_fsync = os.fsync

            for failure_point in ("fstat", "file-fsync", "directory-fsync"):
                with self.subTest(failure_point=failure_point):
                    path = root / f"{failure_point}.lock"
                    directory_fsync_calls = 0

                    def directory_fsync(guard) -> None:
                        nonlocal directory_fsync_calls
                        directory_fsync_calls += 1
                        if failure_point == "directory-fsync" and directory_fsync_calls == 1:
                            raise OSError("primary directory fsync failure")
                        real_directory_fsync(guard)

                    def file_fsync(descriptor: int) -> None:
                        if (
                            failure_point == "file-fsync"
                            and stat.S_ISREG(os.fstat(descriptor).st_mode)
                        ):
                            raise OSError("primary file fsync failure")
                        real_fsync(descriptor)

                    def acquire() -> None:
                        with self.assertRaisesRegex(OSError, "primary"):
                            FileLease(
                                path,
                                "worker",
                                clock=lambda: 100.0,
                                pid_alive=lambda pid: False,
                            ).__enter__()

                    with mock.patch.object(
                        storage_module._ParentDirectoryFlock,
                        "fsync",
                        new=directory_fsync,
                    ), mock.patch(
                        "scripts.loopmem.storage.os.fsync",
                        side_effect=file_fsync,
                    ):
                        if failure_point == "fstat":
                            with mock.patch(
                                "scripts.loopmem.storage.os.fstat",
                                side_effect=OSError("primary fstat failure"),
                            ):
                                acquire()
                        else:
                            acquire()

                    self.assertFalse(path.exists())
                    expected_calls = 2 if failure_point == "directory-fsync" else 1
                    self.assertEqual(directory_fsync_calls, expected_calls)

            with FileLease(
                path,
                "retry-worker",
                clock=lambda: 101.0,
                pid_alive=lambda pid: False,
            ):
                self.assertEqual(read_json(path)["owner"], "retry-worker")

            self.assertFalse(path.exists())

    def test_directory_fsync_covers_lease_reclaim_creation_and_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            self.write_lease(path, expires_at=99.0)
            fsync_kinds: list[str] = []
            real_fsync = os.fsync

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                fsync_kinds.append(
                    "directory" if stat.S_ISDIR(mode) else "file"
                )
                real_fsync(descriptor)

            with mock.patch(
                "scripts.loopmem.storage.os.fsync",
                side_effect=record_fsync,
            ):
                with FileLease(
                    path,
                    "new-worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                ):
                    pass

            self.assertEqual(
                fsync_kinds,
                ["directory", "file", "directory", "directory"],
            )

    def test_unexpired_lease_cannot_be_stolen_when_pid_is_dead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            original = self.write_lease(path, expires_at=101.0)

            self.assert_lease_busy(
                FileLease(
                    path,
                    "new-worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                )
            )

            self.assertEqual(path.read_bytes(), original)

    def test_expired_lease_cannot_be_stolen_when_pid_is_live(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            original = self.write_lease(path, expires_at=99.0)

            self.assert_lease_busy(
                FileLease(
                    path,
                    "new-worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: True,
                )
            )

            self.assertEqual(path.read_bytes(), original)

    def test_expired_lease_with_dead_pid_is_reclaimed_and_removed_on_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            self.write_lease(path, expires_at=99.0)

            with FileLease(
                path,
                "new-worker",
                ttl_seconds=30,
                clock=lambda: 100.0,
                pid_alive=lambda pid: False,
            ):
                current = read_json(path)
                self.assertEqual(current["owner"], "new-worker")
                self.assertEqual(current["pid"], os.getpid())
                self.assertEqual(current["acquired_at"], 100.0)
                self.assertEqual(current["expires_at"], 130.0)
                self.assertNotEqual(current["token"], "old-token")
                self.assertTrue(path.is_file())

            self.assertFalse(path.exists())

    def test_acquisition_clock_is_sampled_under_guard_and_after_reclaim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            self.write_lease(path, expires_at=99.0)
            now = 50.0
            clock_samples: list[float] = []
            original_guard = storage_module._exclusive_parent_flock

            def clock() -> float:
                nonlocal now
                clock_samples.append(now)
                sampled = now
                now += 10.0
                return sampled

            @contextmanager
            def delayed_guard(parent: Path):
                nonlocal now
                with original_guard(parent) as guard:
                    now = 100.0
                    yield guard

            with mock.patch.object(
                storage_module,
                "_exclusive_parent_flock",
                delayed_guard,
            ):
                with FileLease(
                    path,
                    "new-worker",
                    ttl_seconds=30,
                    clock=clock,
                    pid_alive=lambda pid: False,
                ):
                    current = read_json(path)
                    self.assertEqual(current["acquired_at"], 110.0)
                    self.assertEqual(current["expires_at"], 140.0)

            self.assertEqual(clock_samples, [100.0, 110.0])

    def test_corrupt_lease_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            original = b"not json\n"
            path.write_bytes(original)

            with self.assertRaises(LoopMemoryError) as context:
                with FileLease(
                    path,
                    "new-worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                ):
                    self.fail("corrupt lease was acquired")

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(path.read_bytes(), original)

    def test_incomplete_lease_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            original = b'{"owner": "old-worker", "pid": 321}\n'
            path.write_bytes(original)

            with self.assertRaises(LoopMemoryError) as context:
                with FileLease(
                    path,
                    "new-worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                ):
                    self.fail("incomplete lease was acquired")

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(path.read_bytes(), original)

    def test_nonfinite_json_constant_in_lease_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            original = (
                b'{"owner":"old-worker","pid":321,"acquired_at":10.0,'
                b'"expires_at":200.0,"token":"old-token","extra":NaN}\n'
            )
            path.write_bytes(original)

            with self.assertRaises(LoopMemoryError) as context:
                with FileLease(
                    path,
                    "new-worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                ):
                    self.fail("non-finite lease JSON was acquired")

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(path.read_bytes(), original)

    def test_release_does_not_remove_foreign_replacement_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            foreign = {
                "owner": "foreign-worker",
                "pid": 654,
                "acquired_at": 100.0,
                "expires_at": 200.0,
                "token": "foreign-token",
            }

            with FileLease(
                path,
                "original-worker",
                clock=lambda: 100.0,
                pid_alive=lambda pid: False,
            ):
                write_json_atomic(path, foreign)
                foreign_bytes = path.read_bytes()

            self.assertEqual(path.read_bytes(), foreign_bytes)
            self.assertEqual(read_json(path), foreign)

    def test_retryable_release_preserves_ownership_after_unlink_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            lease = FileLease(
                path,
                "worker",
                clock=lambda: 100.0,
                pid_alive=lambda pid: False,
            )
            lease.__enter__()
            real_unlink = Path.unlink
            failed = False

            def fail_once(candidate: Path, *args, **kwargs):
                nonlocal failed
                if candidate == path and not failed:
                    failed = True
                    raise OSError("simulated unlink failure")
                return real_unlink(candidate, *args, **kwargs)

            with mock.patch.object(Path, "unlink", new=fail_once):
                with self.assertRaisesRegex(OSError, "simulated unlink failure"):
                    lease.__exit__(None, None, None)
                self.assertTrue(path.exists())
                lease.__exit__(None, None, None)

            self.assertFalse(path.exists())

    def test_release_directory_fsync_retry_completes_pending_sync(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            lease = FileLease(
                path,
                "worker",
                clock=lambda: 100.0,
                pid_alive=lambda pid: False,
            )
            real_directory_fsync = storage_module._ParentDirectoryFlock.fsync
            directory_fsync_calls = 0

            def fail_release_fsync_once(guard) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls == 2:
                    raise OSError("simulated release directory fsync failure")
                real_directory_fsync(guard)

            with mock.patch.object(
                storage_module._ParentDirectoryFlock,
                "fsync",
                new=fail_release_fsync_once,
            ):
                lease.__enter__()
                with self.assertRaisesRegex(
                    OSError,
                    "simulated release directory fsync failure",
                ):
                    lease.__exit__(None, None, None)

                self.assertFalse(path.exists())
                self.assertTrue(lease._pending_directory_sync)
                self.assertIsNotNone(lease._token)
                self.assertIsNotNone(lease._identity)

                lease.__exit__(None, None, None)

            self.assertEqual(directory_fsync_calls, 3)
            self.assertFalse(lease._pending_directory_sync)
            self.assertIsNone(lease._token)
            self.assertIsNone(lease._identity)

    def test_cooperative_write_racing_release_survives_after_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            foreign = {
                "owner": "foreign-worker",
                "pid": 654,
                "acquired_at": 100.0,
                "expires_at": 200.0,
                "token": "foreign-token",
            }
            lease = FileLease(
                path,
                "original-worker",
                clock=lambda: 100.0,
                pid_alive=lambda pid: False,
            )
            lease.__enter__()

            release_guard_acquired = threading.Event()
            foreign_guard_attempted = threading.Event()
            errors: list[BaseException] = []
            original_guard = storage_module._exclusive_parent_flock
            release_thread: threading.Thread
            foreign_thread: threading.Thread

            @contextmanager
            def observed_guard(parent: Path):
                current = threading.current_thread()
                if current is release_thread:
                    with original_guard(parent) as guard:
                        release_guard_acquired.set()
                        foreign_guard_attempted.wait()
                        yield guard
                elif current is foreign_thread:
                    release_guard_acquired.wait()
                    foreign_guard_attempted.set()
                    with original_guard(parent) as guard:
                        yield guard
                else:
                    with original_guard(parent) as guard:
                        yield guard

            def release() -> None:
                try:
                    lease.__exit__(None, None, None)
                except BaseException as error:
                    errors.append(error)

            def write_foreign_lease() -> None:
                try:
                    write_json_atomic(path, foreign)
                except BaseException as error:
                    errors.append(error)

            release_thread = threading.Thread(target=release, daemon=True)
            foreign_thread = threading.Thread(
                target=write_foreign_lease,
                daemon=True,
            )
            with mock.patch.object(
                storage_module,
                "_exclusive_parent_flock",
                observed_guard,
            ):
                release_thread.start()
                foreign_thread.start()
                release_thread.join(timeout=2)
                foreign_thread.join(timeout=2)

            self.assertFalse(release_thread.is_alive(), "release thread hung")
            self.assertFalse(foreign_thread.is_alive(), "foreign writer hung")
            self.assertEqual(errors, [])
            self.assertEqual(read_json(path), foreign)

    def test_acquisition_uses_exclusive_create(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.lock"
            real_open = Path.open
            calls: list[tuple[Path, str]] = []

            def record_open(candidate: Path, mode: str = "r", *args, **kwargs):
                calls.append((candidate, mode))
                return real_open(candidate, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", new=record_open):
                with FileLease(
                    path,
                    "worker",
                    clock=lambda: 100.0,
                    pid_alive=lambda pid: False,
                ):
                    pass

            self.assertEqual(
                [candidate for candidate, mode in calls if mode == "xb"],
                [path],
            )


if __name__ == "__main__":
    unittest.main()
