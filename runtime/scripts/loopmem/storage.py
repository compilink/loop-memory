from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import time
from typing import TextIO
import uuid

from scripts.loopmem.errors import LoopMemoryError


def ensure_directory(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(path)


ensure_private_dir = ensure_directory


def read_json(path: Path) -> dict[str, object]:
    path = Path(path)
    try:
        value = _strict_json_loads(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as error:
        raise _corrupt_state(path, "invalid JSON") from error

    if not isinstance(value, dict):
        raise _corrupt_state(path, "top-level JSON value is not an object")
    return value


def write_json_atomic(path: Path, value: dict[str, object]) -> None:
    _write_atomic(Path(path), _json_writer(value))


def write_json_atomic_if_unchanged(
    path: Path,
    value: dict[str, object],
    expected: bytes,
) -> bool:
    path = Path(path)
    ensure_directory(path.parent)
    with _exclusive_parent_flock(path.parent) as parent_guard:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return False
        if current != expected:
            return False
        _write_atomic_locked(path, _json_writer(value), parent_guard)
        return True


def _json_writer(value: dict[str, object]) -> Callable[[TextIO], object]:
    def write_value(stream: TextIO) -> None:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")

    return write_value


def write_text_atomic(path: Path, value: str) -> None:
    _write_atomic(Path(path), lambda stream: stream.write(value))


def write_text_atomic_if_unchanged(
    path: Path,
    value: str,
    expected: bytes,
) -> bool:
    path = Path(path)
    ensure_directory(path.parent)
    with _exclusive_parent_flock(path.parent) as parent_guard:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return False
        if current != expected:
            return False
        _write_atomic_locked(
            path,
            lambda stream: stream.write(value),
            parent_guard,
        )
        return True


class _ParentDirectoryFlock:
    def __init__(self, parent: Path) -> None:
        self.parent = parent
        self.descriptor: int | None = None

    def __enter__(self) -> "_ParentDirectoryFlock":
        descriptor = os.open(self.parent, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        descriptor = self.descriptor
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self.descriptor = None

    def fsync(self) -> None:
        if self.descriptor is None:
            raise RuntimeError("parent directory flock is not held")
        os.fsync(self.descriptor)


def _exclusive_parent_flock(parent: Path) -> _ParentDirectoryFlock:
    return _ParentDirectoryFlock(parent)


def _write_atomic(path: Path, write_value: Callable[[TextIO], object]) -> None:
    ensure_directory(path.parent)
    with _exclusive_parent_flock(path.parent) as parent_guard:
        _write_atomic_locked(path, write_value, parent_guard)


def _write_atomic_locked(
    path: Path,
    write_value: Callable[[TextIO], object],
    parent_guard: _ParentDirectoryFlock,
) -> None:
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    identity: tuple[int, int] | None = None

    try:
        stream = temp_path.open(
            "x",
            encoding="utf-8",
            newline="",
        )
        with stream:
            try:
                identity = _identity(os.fstat(stream.fileno()))
            except BaseException:
                try:
                    identity = _identity(os.stat(stream.fileno()))
                except BaseException:
                    pass
                raise
            write_value(stream)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temp_path, path)
        identity = None
        parent_guard.fsync()
    finally:
        if identity is not None:
            _unlink_if_identity(temp_path, identity)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class _LeaseSnapshot:
    value: dict[str, object]
    content: bytes
    identity: tuple[int, int]


class FileLease:
    def __init__(
        self,
        path: Path,
        owner: str,
        ttl_seconds: float = 120,
        clock: Callable[[], float] = time.time,
        pid_alive: Callable[[int], bool] = pid_is_alive,
    ) -> None:
        self.path = Path(path)
        self.owner = owner
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._pid_alive = pid_alive
        self._token: str | None = None
        self._identity: tuple[int, int] | None = None
        self._pending_directory_sync = False

    def __enter__(self) -> "FileLease":
        self._validate_static_creation_inputs()
        ensure_directory(self.path.parent)

        with _exclusive_parent_flock(self.path.parent) as parent_guard:
            while True:
                acquired_at, creation_expires_at = self._sample_creation_times()
                try:
                    self._create(
                        acquired_at,
                        creation_expires_at,
                        parent_guard,
                    )
                    return self
                except FileExistsError:
                    snapshot = _read_lease_snapshot(self.path)
                    if snapshot is None:
                        continue

                    old_expires_at = snapshot.value["expires_at"]
                    old_pid = snapshot.value["pid"]
                    if old_expires_at > acquired_at or self._pid_alive(old_pid):
                        raise LoopMemoryError(
                            code="lease_busy",
                            message=f"Lease is already held: {self.path}",
                        )

                    if _remove_unchanged_snapshot(
                        self.path,
                        snapshot,
                        parent_guard,
                    ):
                        continue

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        release_conclusive = False
        try:
            try:
                with _exclusive_parent_flock(self.path.parent) as parent_guard:
                    release_conclusive = self._release(parent_guard)
            except FileNotFoundError:
                if self.path.exists():
                    raise
                release_conclusive = True
        finally:
            if release_conclusive:
                self._token = None
                self._identity = None
                self._pending_directory_sync = False

    def _validate_static_creation_inputs(self) -> None:
        if not isinstance(self.owner, str) or not self.owner:
            raise _invalid_lease("owner must be a nonempty string")
        if not _is_finite_number(self.ttl_seconds) or self.ttl_seconds <= 0:
            raise _invalid_lease("TTL must be a finite positive number")

    def _sample_creation_times(self) -> tuple[float, float]:
        acquired_at = self._clock()
        if not _is_finite_number(acquired_at):
            raise _invalid_lease("clock must return a finite number")
        expires_at = acquired_at + self.ttl_seconds
        if not _is_finite_number(expires_at) or expires_at <= acquired_at:
            raise _invalid_lease("computed expiry must follow acquisition time")
        return acquired_at, expires_at

    def _create(
        self,
        acquired_at: float,
        expires_at: float,
        parent_guard: _ParentDirectoryFlock,
    ) -> None:
        token = uuid.uuid4().hex
        value = {
            "owner": self.owner,
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "expires_at": expires_at,
            "token": token,
        }
        content = (
            json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
            + b"\n"
        )
        stream = self.path.open("xb")
        identity: tuple[int, int] | None = None

        try:
            try:
                identity = _identity(os.fstat(stream.fileno()))
            except BaseException:
                try:
                    identity = _identity(os.stat(stream.fileno()))
                except BaseException:
                    pass
                raise
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            stream = None
            parent_guard.fsync()
            self._token = token
            self._identity = identity
        except BaseException as primary_error:
            cleanup_error: BaseException | None = None
            try:
                if stream is not None:
                    stream.close()
            except BaseException as error:
                cleanup_error = error

            try:
                unlinked = False
                if identity is not None:
                    unlinked = _unlink_if_identity(self.path, identity)
                if unlinked:
                    parent_guard.fsync()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error

            if cleanup_error is not None:
                raise primary_error from cleanup_error
            raise

    def _release(self, parent_guard: _ParentDirectoryFlock) -> bool:
        if self._pending_directory_sync:
            parent_guard.fsync()
            self._pending_directory_sync = False
            return True
        if self._token is None or self._identity is None:
            return True

        try:
            current_identity = _identity(os.lstat(self.path))
        except FileNotFoundError:
            return True
        if current_identity != self._identity:
            return True

        snapshot = _read_lease_snapshot(self.path)
        if snapshot is None:
            return True
        if snapshot.identity != self._identity:
            return True
        if snapshot.value["token"] != self._token:
            return True
        _remove_unchanged_snapshot(
            self.path,
            snapshot,
            parent_guard,
            before_directory_sync=self._mark_pending_directory_sync,
        )
        self._pending_directory_sync = False
        return True

    def _mark_pending_directory_sync(self) -> None:
        self._pending_directory_sync = True


def _read_lease_snapshot(path: Path) -> _LeaseSnapshot | None:
    try:
        with path.open("rb") as stream:
            content = stream.read()
            identity = _identity(os.fstat(stream.fileno()))
    except FileNotFoundError:
        return None

    try:
        value = _strict_json_loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise _corrupt_state(path, "invalid lease JSON") from error
    if not isinstance(value, dict):
        raise _corrupt_state(path, "lease JSON is not an object")

    _validate_lease(path, value)
    return _LeaseSnapshot(value=value, content=content, identity=identity)


def _validate_lease(path: Path, value: dict[str, object]) -> None:
    required = ("owner", "pid", "acquired_at", "expires_at", "token")
    if any(field not in value for field in required):
        raise _corrupt_state(path, "lease is missing required fields")

    owner = value["owner"]
    pid = value["pid"]
    acquired_at = value["acquired_at"]
    expires_at = value["expires_at"]
    token = value["token"]
    if not isinstance(owner, str) or not owner:
        raise _corrupt_state(path, "lease owner is invalid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise _corrupt_state(path, "lease PID is invalid")
    if not _is_finite_number(acquired_at) or not _is_finite_number(expires_at):
        raise _corrupt_state(path, "lease timestamps are invalid")
    if expires_at <= acquired_at:
        raise _corrupt_state(path, "lease expiry does not follow acquisition")
    if not isinstance(token, str) or not token:
        raise _corrupt_state(path, "lease token is invalid")


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _strict_json_loads(value: bytes) -> object:
    def reject_constant(constant: str) -> object:
        raise ValueError(f"non-finite JSON constant: {constant}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = item
        return result

    return json.loads(
        value,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )


def _remove_unchanged_snapshot(
    path: Path,
    snapshot: _LeaseSnapshot,
    parent_guard: _ParentDirectoryFlock,
    before_directory_sync: Callable[[], None] | None = None,
) -> bool:
    current = _read_lease_snapshot(path)
    if current is None:
        return True
    if current.identity != snapshot.identity or current.content != snapshot.content:
        return False

    try:
        if _identity(os.lstat(path)) != snapshot.identity:
            return False
        path.unlink()
    except FileNotFoundError:
        pass
    else:
        if before_directory_sync is not None:
            before_directory_sync()
        parent_guard.fsync()
    return True


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        if _identity(os.lstat(path)) != identity:
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _identity(stat_result: os.stat_result) -> tuple[int, int]:
    return stat_result.st_dev, stat_result.st_ino


def _corrupt_state(path: Path, detail: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_state",
        message=f"Corrupt state at {path}: {detail}",
        recoverable=False,
    )


def _invalid_lease(detail: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="invalid_lease",
        message=f"Invalid lease: {detail}",
    )
