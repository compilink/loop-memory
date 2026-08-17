import errno
import os
from pathlib import Path
import stat
import uuid

from scripts.loopmem.errors import AccessDenied, LoopMemoryError
from scripts.loopmem.storage import _exclusive_parent_flock


_ACCESS_ERRNOS = frozenset((errno.EACCES, errno.EPERM))
_PROBE_PREFIX = ".loop-memory-access-"
_PROBE_CONTENT = b"loop-memory access probe\n"


def check_access(root: Path, *, materialize_missing: bool = True) -> None:
    root = Path(os.path.abspath(Path(root).expanduser()))
    try:
        _check_access(root, materialize_missing=materialize_missing)
    except OSError as error:
        if isinstance(error, PermissionError) or error.errno in _ACCESS_ERRNOS:
            raise AccessDenied() from error
        raise


def _check_access(root: Path, *, materialize_missing: bool = True) -> None:
    _reject_symlink_components(root)
    try:
        root_value = root.lstat()
    except FileNotFoundError:
        if materialize_missing:
            root.mkdir()
            root_value = root.lstat()
        else:
            parent = root.parent
            parent_value = parent.lstat()
            if stat.S_ISLNK(parent_value.st_mode) or not stat.S_ISDIR(parent_value.st_mode):
                raise LoopMemoryError(
                    code="invalid_loop_root",
                    message="Loop root parent must be a real directory",
                    recoverable=False,
                )
            if parent_value.st_uid != os.getuid():
                raise LoopMemoryError(
                    code="invalid_root_owner",
                    message="Loop root parent is not owned by the current user",
                    recoverable=False,
                )
            if not os.access(parent, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError(parent)
            return

    if stat.S_ISLNK(root_value.st_mode):
        raise LoopMemoryError(
            code="unsafe_path",
            message="Loop root cannot be a symlink",
            recoverable=False,
        )
    if not stat.S_ISDIR(root_value.st_mode):
        raise LoopMemoryError(
            code="invalid_loop_root",
            message="Loop root must be a real directory",
            recoverable=False,
        )
    if root_value.st_uid != os.getuid():
        raise LoopMemoryError(
            code="invalid_root_owner",
            message="Loop root is not owned by the current user",
            recoverable=False,
        )

    list(root.iterdir())
    _probe_read_write_lock_replace(root)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            value = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(value.st_mode):
            raise LoopMemoryError(
                code="unsafe_path",
                message="Loop root path traverses a symlink",
                recoverable=False,
            )


def _probe_read_write_lock_replace(root: Path) -> None:
    probe = root / f"{_PROBE_PREFIX}{uuid.uuid4().hex}"
    original = probe / "original"
    replacement = probe / "replacement"
    primary_error: BaseException | None = None

    try:
        probe.mkdir()
        with original.open("xb") as stream:
            stream.write(_PROBE_CONTENT)
            stream.flush()
            os.fsync(stream.fileno())

        with _exclusive_parent_flock(probe) as parent_guard:
            with replacement.open("xb") as stream:
                stream.write(_PROBE_CONTENT)
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(replacement, original)
            parent_guard.fsync()
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = _close_and_remove_probe(
            probe,
            original,
            replacement,
        )
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def _close_and_remove_probe(
    probe: Path,
    original: Path,
    replacement: Path,
) -> BaseException | None:
    first_error: BaseException | None = None

    for path in (replacement, original):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except BaseException as error:
            if first_error is None:
                first_error = error

    try:
        probe.rmdir()
    except FileNotFoundError:
        pass
    except BaseException as error:
        if first_error is None:
            first_error = error

    return first_error
