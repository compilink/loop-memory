"""Explicit, resumable conversion of Loop Memory metadata to schema v2.

The converter deliberately knows the metadata fields it owns.  Markdown and other
memory bodies are opaque bytes and are never parsed here.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import platform
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
import uuid

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.storage import FileLease, write_json_atomic


_LAYOUT = "relative-paths-v2"
_ROOT_FIELDS = frozenset(("schema_version", "root_id", "owner_uid", "generation", "layout"))
_REGISTRY_V1_FIELDS = frozenset(("schema_version", "projects", "sessions", "legacy_aliases", "maintenance"))
_REGISTRY_V2_FIELDS = frozenset(("schema_version", "generation", "projects", "sessions", "legacy_aliases", "maintenance"))
_MANIFEST_FIELDS = frozenset((
    "migration_id", "schema_version", "state", "source", "source_kind", "project_id",
    "catalogued_files", "files", "target", "created_at", "updated_at", "warnings",
    "protected", "protection_reasons", "hold_reason", "target_files", "classification_sha256",
    "quarantine_path", "staging_path", "publish_plan_sha256", "snapshot",
    "source_inventory_sha256",
))
_MARKER_FIELDS = frozenset((
    "schema_version", "migration_id", "manifest_sha256", "manifest_identity", "phase",
    "quarantine_path", "quarantine_identity", "quarantine_mtime", "staging_path",
    "staging_identity", "staging_mtime",
))
_RELOCATION_FIELDS = frozenset((
    "schema_version", "phase", "old_root", "new_root", "root_id",
))
_RELOCATION_PHASES = (
    "validated",
    "conversion_prepared",
    "conversion_published",
    "root_renamed",
    "complete",
)


@dataclass(frozen=True)
class RootMetadata:
    schema_version: int
    root_id: str
    owner_uid: int
    generation: int
    layout: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "root_id": self.root_id,
            "owner_uid": self.owner_uid,
            "generation": self.generation,
            "layout": self.layout,
        }


@dataclass(frozen=True)
class ConversionFile:
    relative_path: str
    source_bytes: bytes
    source_identity: tuple[int, int]
    converted_bytes: bytes
    converted_sha256: str
    replaced: bool = False


@dataclass(frozen=True)
class ConversionPlan:
    root_metadata: RootMetadata
    files: tuple[ConversionFile, ...]
    noop: bool = False


def relocate_root(
    old_root: Path,
    new_root: Path,
    *,
    fault: Any | None = None,
) -> Path:
    """Resolve or relocate one Loop authority without ever publishing a copy."""
    old = _lexical_absolute(old_root)
    new = _lexical_absolute(new_root)
    if old == new:
        raise _relocation_error(
            "invalid_loop_root",
            "Old and new Loop roots must be distinct",
            recoverable=False,
        )
    # A committed relocation is now the ordinary authority state. Validate its
    # journal and identity without reacquiring the one-time lease in the
    # common parent (which may be outside the granted Loop root).
    if _completed_relocation_is_stable(old, new):
        return new
    lease_path = _relocation_lease_path(old, new)
    with _WaitingRelocationLease(lease_path):
        return _relocate_root_locked(old, new, fault=fault)


def _completed_relocation_is_stable(old: Path, new: Path) -> bool:
    """Return whether a complete relocation can be accepted without a lease."""
    _reject_symlink_components(old)
    _reject_symlink_components(new)
    if _path_exists(old) or not _path_exists(new):
        return False
    transaction = new / "relocation.json"
    if not transaction.exists():
        return False
    _validate_authority_directory(new, allow_v1=False)
    state = _read_relocation(transaction, old, new)
    _validate_relocated_identity(new, state)
    return state["phase"] == "complete"


def _relocate_root_locked(
    old: Path,
    new: Path,
    *,
    fault: Any | None = None,
) -> Path:
    _reject_symlink_components(old)
    _reject_symlink_components(new)
    old_exists = _path_exists(old)
    new_exists = _path_exists(new)

    if old_exists and new_exists:
        _validate_authority_directory(old, allow_v1=True)
        _validate_authority_directory(new, allow_v1=True)
        raise _relocation_error(
            "root_conflict",
            "Both old and new Loop roots contain an authority",
            recoverable=False,
        )
    if not old_exists and not new_exists:
        return new
    if new_exists:
        _validate_authority_directory(new, allow_v1=False)
        transaction = new / "relocation.json"
        if not transaction.exists():
            return new
        state = _read_relocation(transaction, old, new)
        _validate_relocated_identity(new, state)
        if state["phase"] == "complete":
            return new
        if state["phase"] not in {"conversion_published", "root_renamed"}:
            raise _relocation_error(
                "root_transaction_conflict",
                "Relocation phase is inconsistent with the new authority",
                recoverable=False,
            )
        _write_relocation(new, state, "root_renamed")
        _write_relocation(new, state, "complete")
        return new

    _validate_authority_directory(old, allow_v1=True)
    transaction = old / "relocation.json"
    state: dict[str, object]
    if transaction.exists() or transaction.is_symlink():
        state = _read_relocation(transaction, old, new)
        if state["phase"] in {"root_renamed", "complete"}:
            raise _relocation_error(
                "root_transaction_conflict",
                "Relocation phase is inconsistent with the old authority",
                recoverable=False,
            )
    else:
        _assert_relocation_quiescent(old)
        plan = convert_v1_metadata(old)
        validate_conversion_plan(old, plan)
        state = {
            "schema_version": 1,
            "phase": "validated",
            "old_root": str(old),
            "new_root": str(new),
            "root_id": plan.root_metadata.root_id,
        }
        write_json_atomic(transaction, state)

    phase = state["phase"]
    if phase in {"validated", "conversion_prepared"}:
        plan = _relocation_conversion_plan(old, state)
        if plan.root_metadata.root_id != state["root_id"]:
            raise _relocation_error(
                "root_transaction_conflict",
                "Relocation root identity changed during conversion",
                recoverable=False,
            )
        validate_conversion_plan(old, plan)
        _write_relocation(old, state, "conversion_prepared")
        _fault(fault, "before_conversion_publish")
        publish_conversion(old, plan)
        _validate_authority_directory(old, allow_v1=False)
        _write_relocation(old, state, "conversion_published")
        phase = "conversion_published"

    if phase != "conversion_published":
        raise _relocation_error(
            "root_transaction_conflict",
            "Relocation transaction has an unsupported phase",
            recoverable=False,
        )
    _assert_relocation_quiescent(old, allow_conversion_transaction=False)
    _validate_relocated_identity(old, state)
    _validate_rename_filesystem(old, new)
    _fault(fault, "before_root_rename")
    try:
        _native_rename_noreplace(old, new)
    except OSError as error:
        if error.errno == errno.EXDEV:
            raise _relocation_error(
                "root_relocation_cross_device",
                "Loop root relocation requires one filesystem",
            ) from error
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise _relocation_error(
                "root_conflict",
                "The new Loop root appeared during relocation",
                recoverable=False,
            ) from error
        if error.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            raise _relocation_error(
                "root_relocation_unsupported",
                "Filesystem does not provide atomic no-replace directory rename",
            ) from error
        raise
    _fsync_directory(new.parent)
    if old.parent != new.parent:
        _fsync_directory(old.parent)
    _fault(fault, "after_root_rename")
    _write_relocation(new, state, "root_renamed")
    _validate_relocated_identity(new, state)
    _write_relocation(new, state, "complete")
    return new


def _relocation_conversion_plan(
    root: Path,
    state: dict[str, object],
) -> ConversionPlan:
    if not (root / "root.json").exists() and not (
        root / "root.transaction.json"
    ).exists():
        metadata = RootMetadata(
            schema_version=2,
            root_id=str(state["root_id"]),
            owner_uid=os.getuid(),
            generation=1,
            layout=_LAYOUT,
        )
        return _build_conversion_plan(root, recovery_metadata=metadata)
    return convert_v1_metadata(root)


def convert_v1_metadata(root: Path) -> ConversionPlan:
    root = _root_dir(root)
    transaction = root / "root.transaction.json"
    if transaction.exists() or transaction.is_symlink():
        transaction_plan = _plan_from_transaction(root, transaction)
        _verify_recovery_plan(root, transaction_plan)
        return transaction_plan

    return _build_conversion_plan(root)


def _build_conversion_plan(
    root: Path,
    *,
    recovery_metadata: RootMetadata | None = None,
    recovery_files: tuple[ConversionFile, ...] = (),
) -> ConversionPlan:

    metadata_paths = _metadata_paths(root)
    root_path = root / "root.json"
    current_root = _read_optional_json(root_path)
    if current_root is None and recovery_metadata is not None:
        root_metadata = recovery_metadata
    else:
        root_metadata = _convert_root_metadata(current_root, root)
    files: list[ConversionFile] = []
    changed = False
    for path in metadata_paths:
        if not path.exists():
            continue
        value, source_bytes, identity = _read_json_snapshot(path)
        relative = path.relative_to(root).as_posix()
        recovery_item = next(
            (item for item in recovery_files if item.relative_path == relative),
            None,
        )
        if recovery_item is not None and source_bytes == recovery_item.converted_bytes:
            source_bytes = recovery_item.source_bytes
            identity = recovery_item.source_identity
            converted_bytes = recovery_item.converted_bytes
        else:
            converted = _convert_metadata_file(relative, value, root, root_metadata)
            converted_bytes = _json_bytes(converted)
        if converted_bytes != source_bytes and _json_semantically_equal(
            converted_bytes, source_bytes
        ):
            converted_bytes = source_bytes
        if converted_bytes != source_bytes:
            changed = True
        files.append(
            ConversionFile(
                relative_path=relative,
                source_bytes=source_bytes,
                source_identity=identity,
                converted_bytes=converted_bytes,
                converted_sha256=hashlib.sha256(converted_bytes).hexdigest(),
            )
        )

    _rebind_cleanup_marker_metadata(files)

    _validate_known_metadata_files(files)
    _validate_cross_file_references(files)

    # A missing root metadata file is created as known metadata, while an
    # already-v2 root with no other changes is a true no-op.
    if not root_path.exists():
        converted_bytes = _json_bytes(root_metadata.as_dict())
        files.insert(
            0,
            ConversionFile(
                relative_path="root.json",
                source_bytes=b"",
                source_identity=(0, 0),
                converted_bytes=converted_bytes,
                converted_sha256=hashlib.sha256(converted_bytes).hexdigest(),
            ),
        )
        changed = True
    return ConversionPlan(root_metadata, tuple(files), noop=not changed)


def _json_semantically_equal(left: bytes, right: bytes) -> bool:
    try:
        return json.loads(left) == json.loads(right)
    except (UnicodeDecodeError, ValueError):
        return False


def _verify_recovery_plan(root: Path, plan: ConversionPlan) -> None:
    """Prove journal converted bytes are derived from its pinned source baseline."""
    _validate_known_metadata_files(plan.files)
    current_metadata = {
        path.relative_to(root).as_posix()
        for path in _metadata_paths(root)
        if path.exists()
    }
    planned = {item.relative_path for item in plan.files}
    missing = planned - current_metadata
    extra = current_metadata - planned
    if extra or (missing and missing != {"root.json"}):
        raise _conversion_error("recovery plan metadata set is incomplete")
    if not {"root.json", "registry.json"}.issubset(planned):
        raise _conversion_error("recovery plan is missing required metadata")
    expected: list[ConversionFile] = []
    for item in plan.files:
        path = root / PurePosixPath(item.relative_path)
        current_bytes = b""
        current_identity: tuple[int, int] | None = None
        if path.exists():
            current_bytes = path.read_bytes()
            current_identity = _identity(path)
        if current_bytes == item.source_bytes and current_identity == item.source_identity:
            pass
        elif current_bytes != item.converted_bytes:
            raise _conversion_error("recovery source baseline is not proven")
        if item.source_bytes:
            try:
                source_value = json.loads(
                    item.source_bytes.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_keys,
                )
            except (UnicodeDecodeError, ValueError) as error:
                raise _conversion_error("pinned metadata source is invalid") from error
            if not isinstance(source_value, dict):
                raise _conversion_error("pinned metadata source is not an object")
            if item.relative_path != "root.json" and source_value.get("schema_version") != 1:
                raise _conversion_error("recovery source is not the pinned v1 metadata")
            if item.relative_path == "root.json":
                converted = _json_bytes(plan.root_metadata.as_dict())
            else:
                converted = _json_bytes(
                    _convert_metadata_file(
                        item.relative_path,
                        source_value,
                        root,
                        plan.root_metadata,
                    )
                )
        else:
            converted = _json_bytes(plan.root_metadata.as_dict())
        expected.append(
            ConversionFile(
                item.relative_path,
                item.source_bytes,
                item.source_identity,
                converted,
                hashlib.sha256(converted).hexdigest(),
            )
        )
    _rebind_cleanup_marker_metadata(expected)
    _validate_cross_file_references(expected)
    if any(
        left.relative_path != right.relative_path
        or left.source_bytes != right.source_bytes
        or left.source_identity != right.source_identity
        or left.converted_bytes != right.converted_bytes
        or left.converted_sha256 != right.converted_sha256
        for left, right in zip(expected, plan.files, strict=True)
    ):
        raise _conversion_error("recovery converted bytes are not derived from source baseline")


def validate_conversion_plan(root: Path, plan: ConversionPlan) -> None:
    root = _root_dir(root)
    if not isinstance(plan, ConversionPlan):
        raise _conversion_error("conversion plan has an invalid type")
    if plan.root_metadata.schema_version != 2 or plan.root_metadata.layout != _LAYOUT:
        raise _conversion_error("conversion plan root metadata is not schema v2")
    seen: set[str] = set()
    for item in plan.files:
        _validate_relative(item.relative_path)
        if item.relative_path in seen:
            raise _conversion_error("conversion plan contains duplicate files")
        seen.add(item.relative_path)
        path = root / PurePosixPath(item.relative_path)
        if not path.parent.resolve(strict=False).is_relative_to(root):
            raise _conversion_error("conversion plan escapes root")
        if item.source_bytes == b"":
            if path.exists():
                raise _conversion_error("conversion source appeared after planning")
            continue
        try:
            current = path.read_bytes()
            identity = _identity(path)
        except OSError as error:
            raise _conversion_error("conversion source is unavailable") from error
        if current != item.source_bytes or identity != item.source_identity:
            # During recovery a source may already contain exactly the converted
            # bytes; that is an accepted, already-published step.
            if current == item.converted_bytes:
                continue
            raise _conversion_error("conversion source changed after planning")
        if hashlib.sha256(item.converted_bytes).hexdigest() != item.converted_sha256:
            raise _conversion_error("conversion digest is invalid")


def publish_conversion(root: Path, plan: ConversionPlan) -> None:
    root = _root_dir(root)
    validate_conversion_plan(root, plan)
    transaction_path = root / "root.transaction.json"
    if plan.noop and not transaction_path.exists():
        return

    entries = [_transaction_entry(item) for item in plan.files]
    plan_digest = _plan_digest(plan)
    transaction_id = uuid.uuid4().hex
    if transaction_path.exists():
        transaction_plan = _plan_from_transaction(root, transaction_path)
        if not _same_plan(plan, transaction_plan):
            raise _conversion_error("conversion transaction does not match plan")
        tx = _read_transaction(transaction_path)
        entries = tx["entries"]
        plan_digest = tx["plan_digest"]
        transaction_id = tx["transaction_id"]
    else:
        write_json_atomic(transaction_path, {
            "schema_version": 1,
            "root_metadata": plan.root_metadata.as_dict(),
            "plan_digest": plan_digest,
            "transaction_id": transaction_id,
            "entries": entries,
        })

    for entry in entries:
        relative = entry["relative_path"]
        path = root / PurePosixPath(relative)
        converted = base64.b64decode(entry["converted_bytes"])
        if path.exists() and path.read_bytes() == converted:
            entry["replaced"] = True
            continue
        if entry["replaced"]:
            raise _conversion_error("transaction says replaced metadata is missing")
        sibling = path.parent / f".{path.name}.v2-{uuid.uuid4().hex}.tmp"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sibling.open("xb") as stream:
                stream.write(converted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(sibling, path)
            _fsync_directory(path.parent)
        finally:
            if sibling.exists():
                sibling.unlink()
        entry["replaced"] = True
        write_json_atomic(transaction_path, {
            "schema_version": 1,
            "root_metadata": plan.root_metadata.as_dict(),
            "plan_digest": plan_digest,
            "transaction_id": transaction_id,
            "entries": entries,
        })
    try:
        transaction_path.unlink()
        _fsync_directory(root)
    except FileNotFoundError:
        pass


def _metadata_paths(root: Path) -> list[Path]:
    paths: list[Path] = [root / "root.json", root / "registry.json"]
    for directory, pattern in (
        (root / "migrations" / "manifests", "*.json"),
        (root / "migrations" / "maintenance", "*.json"),
    ):
        if directory.is_dir() and not directory.is_symlink():
            paths.extend(sorted(directory.glob(pattern)))
    return paths


def _convert_root_metadata(value: dict[str, object] | None, root: Path) -> RootMetadata:
    if value is None:
        return RootMetadata(2, f"r-{uuid.uuid4().hex}", os.getuid(), 1, _LAYOUT)
    if not isinstance(value, dict):
        raise _conversion_error("root metadata is invalid")
    if set(value) != _ROOT_FIELDS:
        raise _conversion_error("root metadata is partial or has unknown fields")
    if value.get("schema_version") not in (1, 2):
        raise _conversion_error("root metadata schema version is invalid")
    if value.get("layout") not in ("absolute-paths-v1", _LAYOUT):
        raise _conversion_error("root metadata layout is invalid")
    root_id = value.get("root_id")
    if not isinstance(root_id, str) or not root_id.startswith("r-") or not root_id[2:]:
        raise _conversion_error("root metadata root_id is invalid")
    schema_version = value.get("schema_version")
    owner_uid = value.get("owner_uid")
    generation = value.get("generation", 0)
    if schema_version == 2:
        return _root_metadata_from_dict(value)
    if schema_version != 1 or isinstance(owner_uid, bool) or not isinstance(owner_uid, int):
        raise _conversion_error("root metadata v1 fields are invalid")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise _conversion_error("root metadata generation is invalid")
    return RootMetadata(2, root_id, os.getuid(), max(1, generation), _LAYOUT)


def _convert_metadata_file(relative: str, value: dict[str, object], root: Path, metadata: RootMetadata) -> dict[str, object]:
    if relative == "root.json":
        return metadata.as_dict()
    if relative == "registry.json":
        return _convert_registry(value, root)
    if relative.startswith("migrations/manifests/"):
        return _convert_manifest(value, root)
    if relative.startswith("migrations/maintenance/"):
        return _convert_marker(value, root)
    raise _conversion_error("unknown metadata file")


def _rebind_cleanup_marker_metadata(files: list[ConversionFile]) -> None:
    manifests: dict[str, tuple[bytes, str]] = {}
    for item in files:
        if item.relative_path.startswith("migrations/manifests/"):
            migration_id = Path(item.relative_path).stem
            manifests[migration_id] = (
                item.converted_bytes,
                _manifest_fingerprint_bytes(item.converted_bytes),
            )
    for index, item in enumerate(files):
        if not item.relative_path.startswith("migrations/maintenance/"):
            continue
        migration_id = Path(item.relative_path).stem
        manifest = manifests.get(migration_id)
        if manifest is None:
            raise _conversion_error("cleanup marker references unknown manifest")
        value = json.loads(item.converted_bytes.decode("utf-8"))
        value["manifest_sha256"] = manifest[1]
        value.pop("manifest_identity", None)
        value["schema_version"] = 2
        converted = _json_bytes(value)
        files[index] = ConversionFile(
            item.relative_path,
            item.source_bytes,
            item.source_identity,
            converted,
            hashlib.sha256(converted).hexdigest(),
        )


def _manifest_fingerprint_bytes(value: bytes) -> str:
    parsed = json.loads(value.decode("utf-8"))
    canonical = json.dumps(
        parsed,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_cross_file_references(files: list[ConversionFile]) -> None:
    by_path = {
        item.relative_path: json.loads(item.converted_bytes.decode("utf-8"))
        for item in files
    }
    registry = by_path.get("registry.json")
    if not isinstance(registry, dict):
        return
    projects = registry.get("projects", {})
    sessions = registry.get("sessions", {})
    aliases = registry.get("legacy_aliases", {})
    if not isinstance(projects, dict) or not isinstance(sessions, dict):
        raise _conversion_error("registry references are invalid")
    for record in sessions.values():
        if not isinstance(record, dict) or record.get("project_id") not in projects:
            raise _conversion_error("session references unknown project")
        resumes = record.get("resumes_from")
        if resumes is not None and resumes not in sessions:
            raise _conversion_error("session resumes_from references unknown session")
    for relative, value in by_path.items():
        if relative.startswith("migrations/manifests/") and isinstance(value, dict):
            project_id = value.get("project_id")
            if project_id is not None and project_id not in projects:
                raise _conversion_error("manifest references unknown project")
    manifests = {
        value["migration_id"]: value
        for relative, value in by_path.items()
        if relative.startswith("migrations/manifests/")
        and isinstance(value, dict)
        and isinstance(value.get("migration_id"), str)
    }
    if not isinstance(aliases, dict):
        raise _conversion_error("legacy aliases are invalid")
    for source, alias in aliases.items():
        if not isinstance(alias, dict):
            raise _conversion_error("legacy alias is invalid")
        manifest = manifests.get(alias.get("migration_id"))
        if manifest is None:
            raise _conversion_error("legacy alias references unknown migration")
        if source != manifest.get("source") or alias.get("target") != manifest.get("target"):
            raise _conversion_error("legacy alias does not match manifest")


def _convert_registry(value: dict[str, object], root: Path) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value).issubset(_REGISTRY_V1_FIELDS | _REGISTRY_V2_FIELDS):
        raise _conversion_error("registry has unknown fields")
    version = value.get("schema_version")
    if version not in (1, 2):
        raise _conversion_error("registry schema version is invalid")
    for field in ("projects", "sessions", "legacy_aliases", "maintenance"):
        if field not in value or not isinstance(value[field], dict):
            raise _conversion_error(f"registry {field} collection is invalid")
    projects: dict[str, object] = {}
    root_owners: set[str] = set()
    for project_id, record in (value.get("projects") or {}).items():
        if version == 1 and isinstance(record, dict):
            fields = frozenset(record)
            allowed_project_fields = (
                frozenset(("kind", "shared_roots", "roots", "upstream_refs"))
                if fields == frozenset(("kind", "shared_roots", "roots", "upstream_refs"))
                else frozenset(("kind", "common_dirs", "roots", "remotes"))
            )
        else:
            allowed_project_fields = frozenset(("roots",))
        if (
            not isinstance(project_id, str)
            or not project_id.startswith("p-")
            or not isinstance(record, dict)
            or frozenset(record) != allowed_project_fields
            or not isinstance(record.get("roots"), list)
        ):
            raise _conversion_error("project record is invalid")
        if version == 1 and allowed_project_fields == frozenset(("kind", "common_dirs", "roots", "remotes")):
            if record.get("kind") not in ("git", "directory"):
                raise _conversion_error("project kind is invalid")
            for field in ("common_dirs", "roots", "remotes"):
                aliases = record.get(field)
                if not isinstance(aliases, list) or any(
                    not isinstance(alias, str) or not alias for alias in aliases
                    ) or len(aliases) != len(set(aliases)):
                    raise _conversion_error(f"project {field} aliases are invalid")
            if not record["roots"]:
                raise _conversion_error("project roots must not be empty")
            if any(alias.strip() != alias for alias in record["remotes"]):
                raise _conversion_error("project remote alias is not normalized")
            if record["kind"] == "git" and not record["common_dirs"]:
                raise _conversion_error("Git project common_dirs must not be empty")
            if record["kind"] == "directory" and (record["common_dirs"] or record["remotes"]):
                raise _conversion_error("directory project has Git aliases")
        roots: list[str] = []
        for path in record["roots"]:
            absolute = _external_absolute(path, "project root")
            if absolute in roots:
                raise _conversion_error("duplicate project root alias")
            if absolute in root_owners:
                raise _conversion_error("project root alias has multiple owners")
            roots.append(absolute)
            root_owners.add(absolute)
        projects[project_id] = {"roots": roots}
    sessions: dict[str, object] = {}
    platform_records: dict[tuple[str, str], list[tuple[str, dict[str, object]]]] = {}
    for session_id, record in (value.get("sessions") or {}).items():
        allowed_session_fields = (
            frozenset(("project_id", "thread_id"))
            if version == 1
            else frozenset((
                "project_id", "platform_session_id", "generation", "resumes_from", "state"
            ))
        )
        if (
            not isinstance(session_id, str)
            or not session_id.startswith("s-")
            or not isinstance(record, dict)
            or frozenset(record) != allowed_session_fields
            or record.get("project_id") not in projects
        ):
            raise _conversion_error("session record is invalid")
        platform = record.get("platform_session_id", record.get("thread_id"))
        if platform is not None and (not isinstance(platform, str) or not platform.strip()):
            raise _conversion_error("session platform_session_id is invalid")
        key = (record.get("project_id"), platform)
        session_generation = record.get("generation", 1)
        if (
            isinstance(session_generation, bool)
            or not isinstance(session_generation, int)
            or session_generation < 1
        ):
            raise _conversion_error("session generation is invalid")
        resumes_from = record.get("resumes_from")
        if resumes_from is not None and (
            not isinstance(resumes_from, str) or not resumes_from.startswith("s-")
        ):
            raise _conversion_error("session resumes_from is invalid")
        session_state = record.get("state", "active")
        if session_state not in ("active", "archived"):
            raise _conversion_error("session state is invalid")
        sessions[session_id] = {
            "project_id": record.get("project_id"),
            "platform_session_id": platform,
            "generation": session_generation,
            "resumes_from": resumes_from,
            "state": session_state,
        }
        if platform is not None:
            platform_records.setdefault(key, []).append((session_id, sessions[session_id]))
    for records in platform_records.values():
        ordered = sorted(records, key=lambda pair: int(pair[1]["generation"]))
        if [record["generation"] for _, record in ordered] != list(range(1, len(ordered) + 1)):
            raise _conversion_error("session generation chain is discontinuous")
        active = [pair for pair in ordered if pair[1]["state"] == "active"]
        if len(active) > 1 or (active and active[0] != ordered[-1]):
            raise _conversion_error("active session generation is ambiguous")
        for index, (session_id, record) in enumerate(ordered):
            predecessor = None if index == 0 else ordered[index - 1][0]
            if record["resumes_from"] != predecessor:
                raise _conversion_error("session resumes_from chain is invalid")
            if index < len(ordered) - 1 and record["state"] != "archived":
                raise _conversion_error("previous session generation is not archived")
    aliases = value.get("legacy_aliases") or {}
    if not isinstance(aliases, dict):
        raise _conversion_error("legacy_aliases is invalid")
    converted_aliases: dict[str, object] = {}
    normalized_aliases: set[str] = set()
    for source, record in aliases.items():
        if (
            not isinstance(record, dict)
            or frozenset(record) != frozenset(("target", "migration_id"))
        ):
            raise _conversion_error("legacy alias record is invalid")
        target = record.get("target")
        migration_id = record.get("migration_id")
        if not isinstance(target, str):
            raise _conversion_error("legacy alias target is invalid")
        target = _internal_relative(target, root)
        if not isinstance(migration_id, str) or not migration_id.startswith("m-"):
            raise _conversion_error("legacy alias migration_id is invalid")
        normalized_source = _external_absolute(source, "legacy alias source")
        if normalized_source in normalized_aliases:
            raise _conversion_error("legacy alias sources collide after normalization")
        normalized_aliases.add(normalized_source)
        converted_aliases[normalized_source] = {
            "target": target,
            "migration_id": migration_id,
        }
    generation = value.get("generation", 1)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise _conversion_error("registry generation is invalid")
    return {
        "schema_version": 2,
        "generation": generation,
        "projects": projects,
        "sessions": sessions,
        "legacy_aliases": converted_aliases,
        "maintenance": value.get("maintenance", {}),
    }


def _convert_manifest(value: dict[str, object], root: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _conversion_error("migration manifest has unknown fields")
    if "tracked_files" in value:
        if "catalogued_files" in value:
            raise _conversion_error("migration manifest mixes tracking field vocabularies")
        value = dict(value)
        value["catalogued_files"] = value.pop("tracked_files")
    required = frozenset((
        "migration_id", "schema_version", "state", "source", "source_kind",
        "project_id", "catalogued_files", "files", "target", "created_at",
        "updated_at", "warnings",
    ))
    if (
        not required.issubset(value)
        or not set(value).issubset(_MANIFEST_FIELDS)
    ):
        raise _conversion_error("migration manifest has unknown fields")
    result = dict(value)
    if result["state"] not in (
        "detected", "inventoried", "copied", "validated", "references_updated",
        "quarantined", "complete",
    ):
        raise _conversion_error("migration state is invalid")
    project_id = result["project_id"]
    if project_id is not None and (not isinstance(project_id, str) or not project_id.startswith("p-")):
        raise _conversion_error("manifest project_id is invalid")
    for field in ("target", "staging_path", "quarantine_path"):
        if field in result and result[field] is not None:
            result[field] = _internal_relative(result[field], root)
    if "source" in result:
        result["source"] = _external_absolute(result["source"], "legacy source")
    if "snapshot" not in result:
        result["snapshot"] = (
            PurePosixPath("migrations")
            / "quarantine"
            / str(result["migration_id"])
            / "source"
        ).as_posix()
    else:
        result["snapshot"] = _internal_relative(result["snapshot"], root)
    if "source_inventory_sha256" not in result:
        inventory = json.dumps(
            result["files"],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        result["source_inventory_sha256"] = hashlib.sha256(inventory).hexdigest()
    target_files = result.get("target_files")
    if target_files is not None:
        if not isinstance(target_files, list):
            raise _conversion_error("manifest target_files is invalid")
        converted_targets: list[dict[str, object]] = []
        for record in target_files:
            if not isinstance(record, dict) or set(record) != {"relative_path", "sha256", "size"}:
                raise _conversion_error("manifest target_files record is invalid")
            converted = dict(record)
            converted["relative_path"] = _internal_relative(record["relative_path"], root)
            if not isinstance(record["sha256"], str) or len(record["sha256"]) != 64:
                raise _conversion_error("manifest target_files digest is invalid")
            if isinstance(record["size"], bool) or not isinstance(record["size"], int) or record["size"] < 0:
                raise _conversion_error("manifest target_files size is invalid")
            converted_targets.append(converted)
        result["target_files"] = converted_targets
    result["schema_version"] = 2
    return result


def _convert_marker(value: dict[str, object], root: Path) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value).issubset(_MARKER_FIELDS):
        raise _conversion_error("cleanup marker has unknown fields")
    result = dict(value)
    phase = result.get("phase")
    if phase not in {"quarantine_deleting", "quarantine_deleted", "staging_deleting", "complete"}:
        raise _conversion_error("cleanup marker phase is invalid")
    required = {"schema_version", "migration_id", "manifest_sha256", "phase"}
    if result.get("schema_version") == 1:
        required.add("manifest_identity")
    if phase != "complete":
        required.update({
            "quarantine_path", "quarantine_identity", "quarantine_mtime",
            "staging_path", "staging_identity", "staging_mtime",
        })
    if set(result) != required:
        raise _conversion_error("cleanup marker fields are invalid")
    for field in ("quarantine_path", "staging_path"):
        if field in result and result[field] is not None:
            result[field] = _internal_relative(result[field], root)
    result["schema_version"] = 2
    return result


def _internal_relative(value: object, root: Path | None) -> str:
    if not isinstance(value, str) or not value:
        raise _conversion_error("internal path is invalid")
    path = Path(value)
    if not path.is_absolute():
        relative = PurePosixPath(value)
    else:
        if root is None:
            raise _conversion_error("absolute internal alias target is invalid")
        try:
            relative = PurePosixPath(
                path.resolve(strict=False).relative_to(root).as_posix()
            )
        except ValueError as error:
            raise _conversion_error("internal path escapes root") from error
    _validate_relative(relative.as_posix())
    return relative.as_posix()


def _external_absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _conversion_error(f"{label} is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _conversion_error(f"{label} must remain absolute")
    return os.path.normpath(str(path))


def _validate_relative(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or any(part in ("", ".", "..") for part in path.parts):
        raise _conversion_error("internal path escapes root")
    if path.as_posix() != value:
        raise _conversion_error("internal path is not normalized")


def _root_dir(value: Path) -> Path:
    lexical = Path(os.path.abspath(Path(value).expanduser()))
    if lexical.is_symlink() or not lexical.is_dir():
        raise _conversion_error("Loop root must be a real directory")
    return lexical.resolve(strict=False)


def _read_optional_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    value, _, _ = _read_json_snapshot(path)
    return value


def _read_json_snapshot(path: Path) -> tuple[dict[str, object], bytes, tuple[int, int]]:
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise _conversion_error("metadata file cannot be read") from error
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise _conversion_error("metadata file changed while read")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise _conversion_error("metadata JSON is invalid") from error
    if not isinstance(value, dict):
        raise _conversion_error("metadata JSON must be an object")
    return value, raw, (before.st_dev, before.st_ino)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_transaction(path: Path) -> dict[str, Any]:
    value, _, _ = _read_json_snapshot(path)
    _validate_transaction(path.parent, path)
    return value


def _validate_transaction(root: Path, path: Path) -> None:
    value, _, _ = _read_json_snapshot(path)
    if set(value) != {
        "schema_version", "root_metadata", "plan_digest", "transaction_id", "entries"
    }:
        raise _conversion_error("conversion transaction is invalid")
    if value["schema_version"] != 1 or not isinstance(value["entries"], list):
        raise _conversion_error("conversion transaction is invalid")
    root_value = value["root_metadata"]
    if not isinstance(root_value, dict) or set(root_value) != _ROOT_FIELDS:
        raise _conversion_error("conversion transaction root metadata is invalid")
    _root_metadata_from_dict(root_value)
    if not _valid_sha256(value["plan_digest"]):
        raise _conversion_error("conversion transaction plan digest is invalid")
    if (
        not isinstance(value["transaction_id"], str)
        or len(value["transaction_id"]) != 32
        or any(character not in "0123456789abcdef" for character in value["transaction_id"])
    ):
        raise _conversion_error("conversion transaction ID is invalid")
    seen: set[str] = set()
    for entry in value["entries"]:
        required = {
            "relative_path", "source_sha256", "source_identity", "source_bytes",
            "converted_sha256", "converted_bytes", "replaced",
        }
        if not isinstance(entry, dict) or set(entry) != required:
            raise _conversion_error("conversion transaction entry fields are invalid")
        relative = entry["relative_path"]
        if not isinstance(relative, str):
            raise _conversion_error("conversion transaction path is invalid")
        _validate_relative(relative)
        if relative in seen:
            raise _conversion_error("conversion transaction paths are duplicated")
        seen.add(relative)
        candidate = root / PurePosixPath(relative)
        if not candidate.parent.resolve(strict=False).is_relative_to(root):
            raise _conversion_error("conversion transaction path escapes root")
        if not _valid_sha256(entry["source_sha256"]) or not _valid_sha256(entry["converted_sha256"]):
            raise _conversion_error("conversion transaction digest is invalid")
        identity = entry["source_identity"]
        if not isinstance(identity, list) or len(identity) != 2 or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in identity
        ):
            raise _conversion_error("conversion transaction identity is invalid")
        if not isinstance(entry["replaced"], bool):
            raise _conversion_error("conversion transaction phase is invalid")
        try:
            source = base64.b64decode(entry["source_bytes"], validate=True)
            converted = base64.b64decode(entry["converted_bytes"], validate=True)
        except (TypeError, ValueError) as error:
            raise _conversion_error("conversion transaction bytes are invalid") from error
        if hashlib.sha256(source).hexdigest() != entry["source_sha256"]:
            raise _conversion_error("conversion transaction source digest mismatch")
        if hashlib.sha256(converted).hexdigest() != entry["converted_sha256"]:
            raise _conversion_error("conversion transaction converted digest mismatch")


def _transaction_entry(item: ConversionFile) -> dict[str, object]:
    return {
        "relative_path": item.relative_path,
        "source_sha256": hashlib.sha256(item.source_bytes).hexdigest(),
        "source_identity": list(item.source_identity),
        "source_bytes": base64.b64encode(item.source_bytes).decode("ascii"),
        "converted_sha256": item.converted_sha256,
        "converted_bytes": base64.b64encode(item.converted_bytes).decode("ascii"),
        "replaced": item.replaced,
    }


def _plan_from_transaction(root: Path, path: Path) -> ConversionPlan:
    value = _read_transaction(path)
    metadata = _root_metadata_from_dict(value["root_metadata"])
    files: list[ConversionFile] = []
    for entry in value["entries"]:
        source = base64.b64decode(entry["source_bytes"], validate=True)
        converted = base64.b64decode(entry["converted_bytes"], validate=True)
        files.append(ConversionFile(
            relative_path=entry["relative_path"],
            source_bytes=source,
            source_identity=tuple(entry["source_identity"]),
            converted_bytes=converted,
            converted_sha256=entry["converted_sha256"],
            replaced=entry["replaced"],
        ))
    _validate_known_metadata_files(files)
    _validate_cross_file_references(files)
    plan = ConversionPlan(metadata, tuple(files), noop=False)
    if _plan_digest(plan) != value["plan_digest"]:
        raise _conversion_error("conversion transaction plan digest mismatch")
    return plan


def _root_metadata_from_dict(value: dict[str, object]) -> RootMetadata:
    metadata = RootMetadata(
        schema_version=value["schema_version"],
        root_id=value["root_id"],
        owner_uid=value["owner_uid"],
        generation=value["generation"],
        layout=value["layout"],
    )
    if (
        metadata.schema_version != 2
        or not isinstance(metadata.root_id, str)
        or not metadata.root_id.startswith("r-")
        or isinstance(metadata.owner_uid, bool)
        or not isinstance(metadata.owner_uid, int)
        or metadata.owner_uid != os.getuid()
        or isinstance(metadata.generation, bool)
        or not isinstance(metadata.generation, int)
        or metadata.generation < 1
        or metadata.layout != _LAYOUT
    ):
        raise _conversion_error("conversion transaction root metadata is invalid")
    return metadata


def _same_plan(left: ConversionPlan, right: ConversionPlan) -> bool:
    return (
        left.root_metadata == right.root_metadata
        and tuple(
            {**_transaction_entry(item), "replaced": False}
            for item in left.files
        )
        == tuple(
            {**_transaction_entry(item), "replaced": False}
            for item in right.files
        )
    )


def _same_plan_ignoring_phase(left: ConversionPlan, right: ConversionPlan) -> bool:
    return _plan_digest(left) == _plan_digest(right)


def _plan_digest(plan: ConversionPlan) -> str:
    value = {
        "root_metadata": plan.root_metadata.as_dict(),
        "entries": [
            {
                key: value
                for key, value in _transaction_entry(item).items()
                if key != "replaced"
            }
            for item in plan.files
        ],
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _validate_known_metadata_files(files: list[ConversionFile] | tuple[ConversionFile, ...]) -> None:
    for item in files:
        relative = item.relative_path
        path = PurePosixPath(relative)
        known = (
            relative in {"root.json", "registry.json"}
            or (
                len(path.parts) == 3
                and path.parts[0] == "migrations"
                and path.parts[1] in {"manifests", "maintenance"}
                and path.suffix == ".json"
            )
        )
        if not known:
            raise _conversion_error("transaction references non-metadata content")


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _identity(path: Path) -> tuple[int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _conversion_error(detail: str) -> LoopMemoryError:
    return LoopMemoryError(code="conversion_conflict", message=f"Metadata conversion failed: {detail}", recoverable=False)


def _relocation_error(
    code: str,
    detail: str,
    *,
    recoverable: bool = True,
) -> LoopMemoryError:
    return LoopMemoryError(code=code, message=f"Loop root relocation failed: {detail}", recoverable=recoverable)


def _lexical_absolute(value: Path) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def _path_exists(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(value.st_mode):
        raise _relocation_error(
            "invalid_loop_root",
            f"Loop root is not a directory: {path}",
            recoverable=False,
        )
    if stat.S_ISLNK(value.st_mode):
        raise _relocation_error(
            "unsafe_path",
            f"Loop root is a symlink: {path}",
            recoverable=False,
        )
    return True


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            value = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode):
            # macOS exposes /var as a compatibility symlink to /private/var.
            # It is an OS-owned prefix, not a user-controlled authority path.
            if platform.system() == "Darwin" and current == Path("/var"):
                continue
            raise _relocation_error(
                "unsafe_path",
                f"Loop root path traverses a symlink: {path}",
                recoverable=False,
            )


def _validate_authority_directory(root: Path, *, allow_v1: bool) -> None:
    try:
        value = root.lstat()
    except FileNotFoundError as error:
        raise _relocation_error("invalid_loop_root", "Loop root does not exist") from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise _relocation_error("unsafe_path", "Loop root must be a real directory", recoverable=False)
    if value.st_uid != os.getuid():
        raise _relocation_error("invalid_root_owner", "Loop root is not owned by the current user", recoverable=False)
    for name in ("registry.json", "root.json"):
        candidate = root / name
        try:
            child = candidate.lstat()
        except FileNotFoundError as error:
            if name == "root.json" and allow_v1:
                continue
            raise _relocation_error("invalid_loop_root", f"Authority is missing {name}", recoverable=False) from error
        if stat.S_ISLNK(child.st_mode) or not stat.S_ISREG(child.st_mode):
            raise _relocation_error("unsafe_path", f"Authority metadata is unsafe: {name}", recoverable=False)
    try:
        plan = convert_v1_metadata(root)
    except LoopMemoryError as error:
        raise _relocation_error("root_transaction_conflict", str(error), recoverable=False) from error
    if not allow_v1:
        current, _, _ = _read_json_snapshot(root / "root.json")
        if current.get("schema_version") != 2 or not plan.noop:
            raise _relocation_error("root_transaction_conflict", "Authority is not stable schema v2", recoverable=False)


def _assert_relocation_quiescent(
    root: Path,
    *,
    allow_conversion_transaction: bool = True,
) -> None:
    conversion = root / "root.transaction.json"
    for path in (root / "write.transaction.json", root / "relocation.transaction.json"):
        if path.exists() or path.is_symlink():
            raise _relocation_error(
                "root_transaction_conflict",
                f"Unrecognized transaction is present: {path.name}",
                recoverable=False,
            )
    if conversion.exists() and not allow_conversion_transaction:
        raise _relocation_error(
            "root_transaction_conflict",
            "Metadata conversion transaction is incomplete",
            recoverable=False,
        )
    locks = root / "locks"
    if not locks.exists():
        return
    if locks.is_symlink() or not locks.is_dir():
        raise _relocation_error("unsafe_path", "Locks directory is unsafe", recoverable=False)
    now = __import__("time").time()
    for path in locks.iterdir():
        if not path.name.endswith(".lock"):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise _relocation_error("root_relocation_busy", "A lock cannot be validated") from error
        if not isinstance(value, dict):
            raise _relocation_error("root_relocation_busy", "A lock cannot be validated")
        expires = value.get("expires_at")
        pid = value.get("pid")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > now:
            raise _relocation_error("root_relocation_busy", f"Active lease: {path.name}")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            try:
                os.kill(pid, 0)
            except PermissionError:
                raise _relocation_error("root_relocation_busy", f"Active lease: {path.name}")
            except ProcessLookupError:
                pass
            else:
                raise _relocation_error("root_relocation_busy", f"Active lease: {path.name}")


def _read_relocation(path: Path, old: Path, new: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise _relocation_error("root_transaction_conflict", "Relocation journal is invalid", recoverable=False) from error
    if not isinstance(value, dict) or set(value) != _RELOCATION_FIELDS:
        raise _relocation_error("root_transaction_conflict", "Relocation journal fields are invalid", recoverable=False)
    if value.get("schema_version") != 1 or value.get("phase") not in _RELOCATION_PHASES:
        raise _relocation_error("root_transaction_conflict", "Relocation journal phase is invalid", recoverable=False)
    if value.get("old_root") != str(old) or value.get("new_root") != str(new):
        raise _relocation_error("root_transaction_conflict", "Relocation journal paths do not match", recoverable=False)
    if not isinstance(value.get("root_id"), str) or not value["root_id"].startswith("r-"):
        raise _relocation_error("root_transaction_conflict", "Relocation journal identity is invalid", recoverable=False)
    return value


def _write_relocation(root: Path, state: dict[str, object], phase: str) -> None:
    updated = dict(state)
    updated["phase"] = phase
    write_json_atomic(root / "relocation.json", updated)
    state.clear()
    state.update(updated)


def _validate_relocated_identity(root: Path, state: dict[str, object]) -> None:
    value, _, _ = _read_json_snapshot(root / "root.json")
    if value.get("schema_version") != 2 or value.get("root_id") != state.get("root_id"):
        raise _relocation_error("root_transaction_conflict", "Relocated root identity is invalid", recoverable=False)


def _validate_rename_filesystem(old: Path, new: Path) -> None:
    try:
        old_dev = old.stat().st_dev
        parent_dev = new.parent.stat().st_dev
    except OSError as error:
        raise _relocation_error("root_relocation_cross_device", "Cannot verify rename filesystem") from error
    if old_dev != parent_dev:
        raise _relocation_error("root_relocation_cross_device", "Loop roots are on different filesystems")


def _fault(callback: Any | None, point: str) -> None:
    if callback is not None:
        callback(point)


def _native_rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename a directory only when the target does not exist."""
    source_bytes = os.fsencode(str(source))
    target_bytes = os.fsencode(str(target))
    system = platform.system()
    if system == "Linux":
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(-100, source_bytes, -100, target_bytes, 1)
    elif system == "Darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renamex_np", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "renamex_np is unavailable")
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(source_bytes, target_bytes, 0x00000004)
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(source), str(target))


def _relocation_lease_path(old: Path, new: Path) -> Path:
    try:
        common = Path(os.path.commonpath((str(old.parent), str(new.parent))))
    except ValueError as error:
        raise _relocation_error(
            "root_relocation_cross_device",
            "Loop roots do not have a stable common parent",
        ) from error
    return common / ".loop-memory-relocation.lock"


class _WaitingRelocationLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lease: FileLease | None = None

    def __enter__(self) -> FileLease:
        deadline = time.monotonic() + 30.0
        while True:
            lease = FileLease(self.path, owner=f"relocation:{os.getpid()}")
            try:
                lease.__enter__()
            except LoopMemoryError as error:
                if error.code != "lease_busy" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
                continue
            self.lease = lease
            return lease

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        lease = self.lease
        if lease is not None:
            lease.__exit__(exc_type, exc_value, traceback)
            self.lease = None
