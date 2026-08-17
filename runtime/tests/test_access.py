import dataclasses
import errno
import fcntl
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from scripts.loopmem.access import check_access
from scripts.loopmem.errors import AccessDenied, LoopMemoryError, RequiredAccess


class AccessContractTests(unittest.TestCase):
    def test_required_access_is_minimal_and_immutable(self):
        required = RequiredAccess()

        self.assertEqual(
            required.as_dict(),
            {
                "path": "~/loop-memory",
                "read": True,
                "write": True,
                "execute": False,
            },
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            required.write = False

    def test_access_denied_has_fixed_retry_contract(self):
        error = AccessDenied()

        self.assertEqual(error.code, "environment_access_denied")
        self.assertIs(error.recoverable, True)
        self.assertEqual(error.next_action, "request_environment_access")
        self.assertEqual(error.required_access, RequiredAccess())


class AccessProbeTests(unittest.TestCase):
    def assert_no_probe(self, root: Path) -> None:
        if root.exists() and root.is_dir():
            self.assertFalse(
                any(path.name.startswith(".loop-memory-access-") for path in root.iterdir())
            )

    def test_absent_root_is_created_with_normal_mkdir_semantics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"

            check_access(root)

            self.assertTrue(root.is_dir())
            self.assert_no_probe(root)

    def test_existing_readable_writable_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()
            marker = root / "existing-metadata"
            marker.write_bytes(b"metadata-only")

            check_access(root)

            self.assertEqual(marker.read_bytes(), b"metadata-only")
            self.assert_no_probe(root)

    def test_denied_root_creation_requests_loop_root_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            original_mkdir = Path.mkdir

            def deny_selected(path, *args, **kwargs):
                if path == root:
                    raise PermissionError("sensitive root")
                return original_mkdir(path, *args, **kwargs)

            with mock.patch.object(Path, "mkdir", autospec=True, side_effect=deny_selected):
                with self.assertRaises(AccessDenied) as caught:
                    check_access(root)

            self.assertEqual(caught.exception.required_access, RequiredAccess())
            self.assertFalse(root.exists())

    def test_denied_probe_creation_is_typed_and_leaves_no_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()
            original_mkdir = Path.mkdir

            def deny_probe(path, *args, **kwargs):
                if path.parent == root and path.name.startswith(
                    ".loop-memory-access-"
                ):
                    raise PermissionError("sensitive probe")
                return original_mkdir(path, *args, **kwargs)

            with mock.patch.object(
                Path,
                "mkdir",
                autospec=True,
                side_effect=deny_probe,
            ):
                with self.assertRaises(AccessDenied):
                    check_access(root)

            self.assert_no_probe(root)

    def test_denied_lock_is_typed_and_probe_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()

            with mock.patch(
                "scripts.loopmem.storage.fcntl.flock",
                side_effect=PermissionError("sensitive lock"),
            ):
                with self.assertRaises(AccessDenied):
                    check_access(root)

            self.assert_no_probe(root)

    def test_probe_locks_the_same_directory_primitive_as_atomic_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()
            locked_modes = []
            original_flock = fcntl.flock

            def record_locked_descriptor(descriptor, operation):
                if operation == fcntl.LOCK_EX:
                    locked_modes.append(os.fstat(descriptor).st_mode)
                return original_flock(descriptor, operation)

            with mock.patch(
                "scripts.loopmem.storage.fcntl.flock",
                side_effect=record_locked_descriptor,
            ):
                check_access(root)

            self.assertEqual(len(locked_modes), 1)
            self.assertTrue(stat.S_ISDIR(locked_modes[0]))
            self.assert_no_probe(root)

    def test_denied_atomic_replace_requests_only_loop_root_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()

            with mock.patch(
                "scripts.loopmem.access.os.replace",
                side_effect=PermissionError("sensitive replacement"),
            ):
                with self.assertRaises(AccessDenied) as caught:
                    check_access(root)

            self.assertEqual(caught.exception.required_access.path, "~/loop-memory")
            self.assertTrue(caught.exception.required_access.read)
            self.assertTrue(caught.exception.required_access.write)
            self.assertFalse(caught.exception.required_access.execute)
            self.assert_no_probe(root)

    def test_denied_fsync_is_typed_and_probe_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()

            with mock.patch(
                "scripts.loopmem.access.os.fsync",
                side_effect=PermissionError("sensitive fsync"),
            ):
                with self.assertRaises(AccessDenied):
                    check_access(root)

            self.assert_no_probe(root)

    def test_eacces_and_eperm_are_typed_as_access_denied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()

            for error_number in (errno.EACCES, errno.EPERM):
                with self.subTest(error_number=error_number), mock.patch(
                    "scripts.loopmem.access.os.replace",
                    side_effect=OSError(error_number, "sensitive replacement"),
                ):
                    with self.assertRaises(AccessDenied):
                        check_access(root)
                self.assert_no_probe(root)

    def test_symlink_root_remains_a_typed_safety_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            target = temp / "target"
            target.mkdir()
            root = temp / "loop-memory"
            root.symlink_to(target, target_is_directory=True)

            with self.assertRaises(LoopMemoryError) as caught:
                check_access(root)

            self.assertEqual(caught.exception.code, "unsafe_path")
            self.assertIs(caught.exception.recoverable, False)
            self.assert_no_probe(target)

    def test_wrong_owner_remains_a_typed_safety_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()

            with mock.patch(
                "scripts.loopmem.access.os.getuid",
                return_value=os.getuid() + 1,
            ):
                with self.assertRaises(LoopMemoryError) as caught:
                    check_access(root)

            self.assertEqual(caught.exception.code, "invalid_root_owner")
            self.assertIs(caught.exception.recoverable, False)
            self.assert_no_probe(root)

    def test_unrelated_io_error_is_not_mislabeled_as_access_denied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()
            failure = OSError(errno.EIO, "sensitive device detail")

            with mock.patch("scripts.loopmem.access.os.replace", side_effect=failure):
                with self.assertRaises(OSError) as caught:
                    check_access(root)

            self.assertIs(caught.exception, failure)
            self.assert_no_probe(root)


if __name__ == "__main__":
    unittest.main()
