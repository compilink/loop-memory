"""Read-only custody for legacy memory trees.

The source tree is never moved or removed.  A stage is a verified copy held
under the canonical Loop root; only that retained copy can later be deleted.
"""

import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import time
import uuid

from scripts.loopmem import migration
from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.paths import assert_loop_path, is_reserved_product_path
from scripts.loopmem.storage import FileLease, ensure_directory, read_json, write_json_atomic


_SNAPSHOT_ID = re.compile(r"^l-[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def stage_legacy(loop_root: Path, cwd: Path) -> dict[str, object]:
    root = _safe_root(loop_root)
    ensure_directory(root)
    source = _source_path(Path(cwd))
    _validate_source(source)
    _reject_overlap(root, source)

    first_files, has_credentials = migration._inventory_files(source)
    digest = migration.inventory_sha256(first_files)
    importable = not has_credentials
    reasons = ["credential_assignment"] if has_credentials else []

    # Serialize staging for one source/inventory.  The initial inventory is
    # deliberately outside the lease so unrelated callers can fail fast on an
    # unsafe source; the lease protects the check-and-create section.
    lease = assert_loop_path(root, root / "locks" / f"legacy-stage-{digest}.lock")
    with _wait_for_lease(lease, "legacy-stage"):
        existing = _find_receipt(root, source, digest)
        if existing is not None:
            return existing

        first_identities = _source_identities(source)
        snapshot_id = f"l-{uuid.uuid4().hex}"
        snapshot_dir = assert_loop_path(root, root / "legacy-snapshots" / snapshot_id)
        payload = assert_loop_path(root, snapshot_dir / "payload")
        receipt_path = assert_loop_path(root, snapshot_dir / "receipt.json")
        ensure_directory(snapshot_dir)
        snapshot_identity = _path_identity(snapshot_dir)
        try:
            _copy_source(source, payload)
            second_files, second_credentials = migration._inventory_files(source)
            second_digest = migration.inventory_sha256(second_files)
            second_identities = _source_identities(source)
            if (
                second_digest != digest
                or second_credentials != has_credentials
                or second_identities != first_identities
            ):
                raise LoopMemoryError(
                    code="source_unstable",
                    message="Legacy source changed during staging",
                    recoverable=True,
                )
            _validate_payload(payload, digest)
            receipt = {
                "schema_version": 2,
                "snapshot_id": snapshot_id,
                "source_path": str(source),
                "inventory_sha256": digest,
                "importable": importable,
                "protection_reasons": reasons,
            }
            write_json_atomic(receipt_path, receipt)
        except LoopMemoryError:
            _remove_tree_if_identity(snapshot_dir, snapshot_identity)
            raise
        except OSError as error:
            _remove_tree_if_identity(snapshot_dir, snapshot_identity)
            raise LoopMemoryError(
                code="legacy_stage_failed",
                message="Legacy memory could not be copied safely",
                recoverable=False,
            ) from error

        return _stage_result(snapshot_id, source, payload, receipt_path, digest, importable)


def delete_legacy(loop_root: Path, snapshot_id: str) -> dict[str, object]:
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise LoopMemoryError(
            code="invalid_legacy_snapshot_id",
            message="Legacy snapshot ID is invalid",
            recoverable=False,
        )
    root = _safe_root(loop_root)
    snapshot_dir = assert_loop_path(root, root / "legacy-snapshots" / snapshot_id)
    receipt_path = assert_loop_path(root, snapshot_dir / "receipt.json")
    payload = assert_loop_path(root, snapshot_dir / "payload")
    lease_path = assert_loop_path(
        root,
        root / "locks" / f"legacy-delete-{snapshot_id}.lock",
    )
    with _wait_for_lease(lease_path, "legacy-delete"):
        snapshot_identity = _directory_identity(snapshot_dir)
        receipt, receipt_identity, receipt_content = _load_receipt_snapshot(
            receipt_path,
            snapshot_id,
        )
        source = Path(receipt["source_path"])
        if not _lexists(payload):
            tombstone = _recoverable_delete_tombstone(
                snapshot_dir,
                str(receipt["inventory_sha256"]),
            )
            _verify_delete_chain(
                snapshot_dir,
                snapshot_identity,
                receipt_path,
                receipt_identity,
                receipt_content,
                snapshot_id,
                snapshot_metadata_may_change=True,
            )
            if tombstone is not None:
                tombstone_identity = _directory_identity(tombstone)
                _remove_tree_if_identity(tombstone, tombstone_identity)
                if _lexists(tombstone):
                    raise _unsafe_snapshot_state()
                return _delete_result(
                    snapshot_id,
                    source,
                    payload,
                    receipt_path,
                    True,
                )
            return _delete_result(snapshot_id, source, payload, receipt_path, False)
        payload_identity = _directory_identity(payload)
        _validate_payload(payload, str(receipt["inventory_sha256"]))
        _verify_delete_chain(
            snapshot_dir,
            snapshot_identity,
            receipt_path,
            receipt_identity,
            receipt_content,
            snapshot_id,
            payload,
            payload_identity,
        )
        tombstone = assert_loop_path(
            root,
            snapshot_dir / f".payload-delete-{uuid.uuid4().hex}",
        )
        try:
            migration._rename_no_replace(payload, tombstone)
            moved_identity = _directory_identity(tombstone)
            if moved_identity[:4] != payload_identity[:4]:
                _restore_unexpected_tombstone(tombstone, payload)
                raise _unsafe_snapshot_state()
            tombstone_identity = moved_identity
            _verify_delete_chain(
                snapshot_dir,
                snapshot_identity,
                receipt_path,
                receipt_identity,
                receipt_content,
                snapshot_id,
                snapshot_metadata_may_change=True,
            )
            _remove_tree_if_identity(tombstone, tombstone_identity)
            if _lexists(tombstone):
                raise _unsafe_snapshot_state()
        except LoopMemoryError:
            raise
        except OSError as error:
            raise LoopMemoryError(
                code="legacy_delete_failed",
                message="The selected legacy snapshot could not be deleted",
                recoverable=False,
            ) from error
        return _delete_result(snapshot_id, source, payload, receipt_path, True)


def has_staged_receipt(
    loop_root: Path,
    source: Path,
    inventory_digest: str | None = None,
) -> bool:
    source = Path(source).expanduser().resolve(strict=False)
    snapshots = Path(loop_root).expanduser().resolve(strict=False) / "legacy-snapshots"
    try:
        value = snapshots.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise _unsafe_snapshot_state()
        entries = list(snapshots.iterdir())
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _unsafe_snapshot_state() from error
    for snapshot_dir in entries:
        if not _SNAPSHOT_ID.fullmatch(snapshot_dir.name):
            continue
        value = snapshot_dir.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise _unsafe_snapshot_state()
        receipt = _load_receipt(snapshot_dir / "receipt.json", snapshot_dir.name)
        if Path(receipt["source_path"]) != source:
            continue
        if inventory_digest is not None and receipt["inventory_sha256"] != inventory_digest:
            continue
        payload = snapshot_dir / "payload"
        if _lexists(payload):
            _validate_payload(payload, str(receipt["inventory_sha256"]))
        return True
    return False


def _source_path(cwd: Path) -> Path:
    candidate = cwd.expanduser().resolve(strict=False)
    return candidate if candidate.name == ".memory" else candidate / ".memory"


def _validate_source(source: Path) -> None:
    try:
        value = source.lstat()
    except FileNotFoundError as error:
        raise LoopMemoryError(
            code="legacy_memory_not_found",
            message="The current project has no legacy memory directory",
        ) from error
    except OSError as error:
        raise _unsafe_snapshot_state() from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise LoopMemoryError(
            code="unsafe_legacy_source",
            message="Legacy memory must be a real directory",
            recoverable=False,
        )


def _copy_source(source: Path, payload: Path) -> None:
    ensure_directory(payload)
    source_stat = migration._source_root_stat(source, missing_ok=False)
    if source_stat is None:
        raise migration._source_unstable()
    for directory_fd, name, relative_path, entry_stat in migration._descriptor_entries(
        source, source_stat, validate_paths=True
    ):
        destination = payload.joinpath(*PurePosixPath(relative_path).parts)
        ensure_directory(destination.parent)
        content = migration._read_inventory_file(directory_fd, name, entry_stat)
        with destination.open("xb") as target_file:
            target_file.write(content)
            target_file.flush()
            os.fsync(target_file.fileno())


def _source_identities(source: Path) -> dict[str, tuple[int, int, int, int, int, int]]:
    source_stat = migration._source_root_stat(source, missing_ok=False)
    if source_stat is None:
        raise migration._source_unstable()
    identities = {"": migration._full_identity(source_stat)}
    for _, _, relative_path, entry_stat in migration._descriptor_entries(
        source, source_stat, validate_paths=True
    ):
        identities[relative_path] = migration._full_identity(entry_stat)
    return identities


def _path_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        return migration._full_identity(path.lstat())
    except OSError as error:
        raise _unsafe_snapshot_state() from error


def _remove_tree_if_identity(root: Path, expected_identity) -> None:
    try:
        current = root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if migration._node_identity(current) != expected_identity[:3]:
        return
    _remove_tree(root)


class _WaitingFileLease:
    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        self.lease: FileLease | None = None

    def __enter__(self) -> FileLease:
        deadline = time.monotonic() + 120
        while True:
            lease = FileLease(self.path, owner=self.owner)
            try:
                lease.__enter__()
            except LoopMemoryError as error:
                if error.code != "lease_busy" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
                continue
            self.lease = lease
            return lease

    def __exit__(self, error_type, error, traceback) -> None:
        if self.lease is not None:
            self.lease.__exit__(error_type, error, traceback)
            self.lease = None


def _wait_for_lease(path: Path, owner: str) -> _WaitingFileLease:
    return _WaitingFileLease(path, owner)


def _find_receipt(root: Path, source: Path, digest: str) -> dict[str, object] | None:
    snapshots = root / "legacy-snapshots"
    if not snapshots.exists():
        return None
    for directory in sorted(snapshots.iterdir(), key=lambda item: item.name):
        if not _SNAPSHOT_ID.fullmatch(directory.name):
            continue
        receipt_path = directory / "receipt.json"
        try:
            receipt = _load_receipt(receipt_path, directory.name)
        except LoopMemoryError:
            continue
        if Path(receipt["source_path"]) != source or receipt["inventory_sha256"] != digest:
            continue
        payload = directory / "payload"
        try:
            _validate_payload(payload, digest)
        except LoopMemoryError:
            continue
        return _stage_result(
            directory.name,
            source,
            payload,
            receipt_path,
            digest,
            bool(receipt.get("importable", True)),
        )
    return None


def _load_receipt(path: Path, snapshot_id: str) -> dict[str, object]:
    receipt, _, _ = _load_receipt_snapshot(path, snapshot_id)
    return receipt


def _load_receipt_snapshot(
    path: Path,
    snapshot_id: str,
) -> tuple[dict[str, object], tuple[int, int, int, int, int, int], bytes]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise _unsafe_snapshot_state()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            identity = migration._full_identity(opened)
            if identity != migration._full_identity(before):
                raise _unsafe_snapshot_state()
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            live = path.lstat()
            if (
                migration._full_identity(after) != identity
                or migration._full_identity(live) != identity
                or len(content) != opened.st_size
            ):
                raise _unsafe_snapshot_state()
        finally:
            os.close(descriptor)
        receipt = json.loads(content)
        if not isinstance(receipt, dict):
            raise _unsafe_snapshot_state()
    except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError) as error:
        raise _unsafe_snapshot_state() from error
    source_value = receipt.get("source_path")
    source = Path(source_value) if isinstance(source_value, str) else None
    digest = receipt.get("inventory_sha256")
    if (
        receipt.get("schema_version") not in (1, 2)
        or receipt.get("snapshot_id") != snapshot_id
        or source is None
        or not source.is_absolute()
        or source != Path(os.path.abspath(source.expanduser()))
        or is_reserved_product_path(source)
        or not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
    ):
        raise _unsafe_snapshot_state()
    return receipt, identity, content


def _directory_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        value = path.lstat()
    except OSError as error:
        raise _unsafe_snapshot_state() from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise _unsafe_snapshot_state()
    return migration._full_identity(value)


def _verify_delete_chain(
    snapshot_dir: Path,
    snapshot_identity,
    receipt_path: Path,
    receipt_identity,
    receipt_content: bytes,
    snapshot_id: str,
    payload: Path | None = None,
    payload_identity=None,
    snapshot_metadata_may_change: bool = False,
) -> None:
    current_snapshot_identity = _directory_identity(snapshot_dir)
    if (
        current_snapshot_identity[:3] != snapshot_identity[:3]
        if snapshot_metadata_may_change
        else current_snapshot_identity != snapshot_identity
    ):
        raise _unsafe_snapshot_state()
    _, current_receipt_identity, current_receipt_content = _load_receipt_snapshot(
        receipt_path,
        snapshot_id,
    )
    if (
        current_receipt_identity != receipt_identity
        or current_receipt_content != receipt_content
    ):
        raise _unsafe_snapshot_state()
    if payload is not None and _directory_identity(payload) != payload_identity:
        raise _unsafe_snapshot_state()


def _restore_unexpected_tombstone(tombstone: Path, payload: Path) -> None:
    if _lexists(payload):
        return
    try:
        migration._rename_no_replace(tombstone, payload)
    except (OSError, LoopMemoryError):
        return


def _recoverable_delete_tombstone(
    snapshot_dir: Path,
    expected_digest: str,
) -> Path | None:
    try:
        candidates = sorted(snapshot_dir.glob(".payload-delete-*"))
    except OSError as error:
        raise _unsafe_snapshot_state() from error
    if not candidates:
        return None
    if len(candidates) != 1 or not re.fullmatch(
        r"\.payload-delete-[0-9a-f]{32}",
        candidates[0].name,
    ):
        raise _unsafe_snapshot_state()
    candidate = candidates[0]
    _directory_identity(candidate)
    _validate_payload(candidate, expected_digest)
    return candidate


def _validate_payload(payload: Path, expected_digest: str) -> None:
    try:
        value = payload.lstat()
    except OSError as error:
        raise _unsafe_snapshot_state() from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise _unsafe_snapshot_state()
    try:
        actual_digest = migration.inventory_sha256(payload)
    except LoopMemoryError as error:
        raise _unsafe_snapshot_state() from error
    if actual_digest != expected_digest:
        raise LoopMemoryError(
            code="legacy_snapshot_changed",
            message="Legacy snapshot integrity has changed",
            recoverable=False,
        )


def _stage_result(
    snapshot_id: str,
    source: Path,
    payload: Path,
    receipt: Path,
    digest: str,
    importable: bool,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "source_path": str(source),
        "snapshot_path": str(payload),
        "receipt_path": str(receipt),
        "inventory_sha256": digest,
        "importable": importable,
    }


def _delete_result(snapshot_id, source, payload, receipt, deleted):
    return {
        "snapshot_id": snapshot_id,
        "source_path": str(source),
        "snapshot_path": str(payload),
        "receipt_path": str(receipt),
        "deleted": deleted,
    }


def _validate_payload_root(root: Path) -> None:
    value = root.lstat()
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise _unsafe_snapshot_state()


def _remove_tree(root: Path) -> None:
    if not _lexists(root):
        return
    _validate_payload_root(root)
    for entry in list(root.iterdir()):
        value = entry.lstat()
        if stat.S_ISLNK(value.st_mode) or stat.S_ISREG(value.st_mode):
            entry.unlink()
        elif stat.S_ISDIR(value.st_mode):
            _remove_tree(entry)
        else:
            raise _unsafe_snapshot_state()
    root.rmdir()


def _safe_root(loop_root: Path) -> Path:
    root = Path(loop_root).expanduser().resolve(strict=False)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Legacy custody cannot use product-owned memory paths",
            recoverable=False,
        )
    return root


def _reject_overlap(root: Path, source: Path) -> None:
    if _is_relative_to(source, root) or _is_relative_to(root, source):
        raise LoopMemoryError(
            code="unsafe_legacy_source",
            message="Loop root and project legacy memory must not overlap",
            recoverable=False,
        )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _unsafe_snapshot_state() from error
    return True


def _unsafe_snapshot_state() -> LoopMemoryError:
    return LoopMemoryError(
        code="unsafe_legacy_snapshot",
        message="Legacy snapshot metadata or payload is unsafe",
        recoverable=False,
    )
