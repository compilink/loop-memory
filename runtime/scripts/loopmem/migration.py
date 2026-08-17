import ctypes
from collections.abc import Callable
from contextlib import closing, ExitStack
from dataclasses import dataclass, field
import hashlib
from datetime import datetime
import errno
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import uuid

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import global_facts
from scripts.loopmem.paths import (
    assert_loop_path,
    discover_project,
    is_reserved_product_path,
)
from scripts.loopmem.registry import RegistryStore
from scripts.loopmem.sessions import (
    PROJECT_SECTIONS,
    ensure_global_layout,
    ensure_project_layout,
)
from scripts.loopmem.storage import (
    FileLease,
    ensure_directory,
    read_json,
    write_json_atomic,
    write_json_atomic_if_unchanged,
    write_text_atomic,
)


_BASE_FIELDS = frozenset(
    (
        "migration_id",
        "schema_version",
        "state",
        "source",
        "source_kind",
        "project_id",
        "catalogued_files",
        "files",
        "target",
        "created_at",
        "updated_at",
        "warnings",
    )
)
_OPERATIONAL_FIELDS = frozenset(
    (
        "protected",
        "protection_reasons",
        "hold_reason",
        "target_files",
        "classification_sha256",
        "quarantine_path",
        "staging_path",
        "publish_plan_sha256",
        "snapshot",
        "source_inventory_sha256",
    )
)
_REFRESH_FORBIDDEN_FIELDS = frozenset(
    (
        "classification_sha256",
        "staging_path",
        "quarantine_path",
        "target_files",
        "publish_plan_sha256",
        "hold_reason",
    )
)
_MIGRATION_ID = re.compile(r"^m-[0-9a-f]{32}$")
_LEGACY_SNAPSHOT = re.compile(r"^legacy-snapshots/l-[0-9a-f]{32}/payload$")
_V1_QUARANTINE_SNAPSHOT = re.compile(
    r"^migrations/quarantine/(m-[0-9a-f]{32})/source$"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^p-[A-Za-z0-9][A-Za-z0-9._-]*$")
_CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?im)(?:"
    rb"^[ \t]*(?:[-*][ \t]+)?(?:export[ \t]+)?[\"']?"
    rb"(?:(?!CASE_)(?:[A-Z][A-Z0-9_]*_)?TOKEN"
    rb"|(?:[A-Z][A-Z0-9_]*_)?(?:SECRET|PASSWORD)"
    rb"|[A-Z][A-Z0-9_]*_KEY)[\"']?[ \t]*[:=]"
    rb"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    rb"|^[ \t]*[\"']?Authorization[\"']?[ \t]*[:=][ \t]*"
    rb"[\"']?Bearer[ \t]+[^\s\"']+"
    rb")"
)
_PROJECT_AUTHORITY_SOURCES = frozenset(
    (
        "project.md",
        "project/long.md",
        "project/medium.md",
        "project/short.md",
    )
)
_ARCHIVE_MONTH = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_SELECTION_ENVIRONMENT = (
    "EXT_DIR",
    "EXT_WORKSPACE_TREE",
    "EXT_SHARED_DIR",
    "EXT_INDEX_FILE",
    "EXT_OBJECT_DIRECTORY",
    "EXT_ALTERNATE_OBJECT_DIRECTORIES",
    "EXT_CEILING_DIRECTORIES",
    "EXT_DISCOVERY_ACROSS_FILESYSTEM",
)
_STATES = (
    "detected",
    "inventoried",
    "copied",
    "validated",
    "references_updated",
    "quarantined",
    "complete",
)
_STATE_INDEX = {state: index for index, state in enumerate(_STATES)}
_LEDGER_EVENT_FIELDS = frozenset(("migration_id", "state", "timestamp"))
_SCANDIR_FD_SUPPORTED = os.scandir in getattr(os, "supports_fd", ())
_OPEN_DIR_FD_SUPPORTED = os.open in getattr(os, "supports_dir_fd", ())
_STAT_DIR_FD_SUPPORTED = os.stat in getattr(os, "supports_dir_fd", ())
_NOFOLLOW_STAT_FUNCTION = os.stat


@dataclass(frozen=True)
class ClassificationSnapshot:
    source_path: Path
    content: bytes = field(repr=False)
    sha256: str


@dataclass(frozen=True)
class SourceSnapshot:
    files: list[dict[str, object]]
    catalogued_files: list[str]
    has_credential_assignment: bool


def inspect_project_legacy_source(cwd: Path) -> dict[str, object]:
    """Return a stable, body-free snapshot of the current project's .memory."""
    current = Path(cwd).expanduser().resolve(strict=False)
    discovery = discover_project(current)
    project_root = discovery.root if discovery.kind == "repository" else current
    source = _absolute_lexical(project_root / ".memory")
    source_stat = _source_root_stat(source, missing_ok=True)
    if source_stat is None:
        return {"exists": False, "source": str(source)}
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
        raise _unsafe_legacy_source(
            "Project legacy memory must be a real directory"
        )
    snapshot = _stable_source_snapshot(source, "project")
    return {
        "exists": True,
        "source": str(source),
        "inventory_sha256": _inventory_sha256(snapshot.files),
        "catalogued_files": snapshot.catalogued_files,
        "has_credential_assignment": snapshot.has_credential_assignment,
    }


def inventory_sha256(value: Path | object) -> str:
    """Return the deterministic digest for one tree or validated inventory."""
    if isinstance(value, Path):
        files, _ = _inventory_files(value)
        return _inventory_sha256(files)
    return _inventory_sha256(value)


def scan_legacy(
    loop_root: Path,
    cwd: Path,
    candidates: list[Path],
) -> dict[str, object]:
    lexical_root = _absolute_lexical(loop_root)
    if is_reserved_product_path(lexical_root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory scans cannot target product-owned memory",
            recoverable=False,
        )
    root = lexical_root.resolve(strict=False)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory scans cannot target product-owned memory",
            recoverable=False,
        )
    current = Path(cwd).expanduser().resolve(strict=False)
    cwd_discovery = discover_project(current)
    implicit_source = (
        cwd_discovery.root / ".memory"
        if cwd_discovery.kind == "repository"
        else current / ".memory"
    )
    manifests_dir = assert_loop_path(root, root / "migrations" / "manifests")
    _reject_loop_source_overlap(root, [implicit_source, *candidates])
    existing_paths = sorted(manifests_dir.glob("*.json"))
    existing_sources = [
        Path(load_manifest(path)["source"])
        for path in existing_paths
    ]
    _reject_loop_source_overlap(root, existing_sources)

    registry = RegistryStore(root)
    registry.initialize()
    ensure_directory(manifests_dir)
    lease_path = assert_loop_path(root, root / "locks" / "migration.lock")

    with FileLease(lease_path, owner="migration-scan"):
        existing: dict[str, Path] = {}
        for path in sorted(manifests_dir.glob("*.json")):
            manifest = load_manifest(path)
            source = manifest["source"]
            _reject_loop_source_overlap(root, [Path(source)])
            if source in existing:
                raise _corrupt_manifest(
                    f"multiple manifests claim the same source: {source}"
                )
            existing[source] = path.resolve(strict=False)

        warnings: list[str] = []
        excluded: list[dict[str, str]] = []
        manifest_paths: list[str] = []
        seen: set[str] = set()
        requested = [implicit_source, *candidates]
        requested.extend(Path(source) for source in existing)
        for candidate in requested:
            lexical_candidate = _absolute_lexical(candidate)
            if is_reserved_product_path(lexical_candidate):
                excluded.append(
                    {
                        "path": str(lexical_candidate),
                        "reason": "reserved_product_memory",
                    }
                )
                continue
            source = _containing_memory_root(candidate)
            if source is None:
                excluded.append(
                    {
                        "path": str(_absolute_lexical(candidate)),
                        "reason": "not_legacy_memory",
                    }
                )
                continue
            lexical_source = str(source)
            if _contains_symlink(source):
                if lexical_source not in seen:
                    seen.add(lexical_source)
                    excluded.append(
                        {
                            "path": lexical_source,
                            "reason": "unsafe_legacy_source",
                        }
                    )
                continue
            source_key = str(source.resolve(strict=False))
            if source_key in seen:
                continue
            seen.add(source_key)
            if source_key in existing:
                manifest_path = existing[source_key]
                manifest_paths.append(str(manifest_path))
                manifest = load_manifest(manifest_path)
                if manifest["state"] == "inventoried":
                    _validate_ledger(root, manifest)
                    _ensure_ledger_event(
                        root,
                        manifest["migration_id"],
                        "inventoried",
                    )
                continue
            if not source.exists():
                warnings.append(f"Missing legacy source skipped: {source}")
                continue
            if not source.is_dir():
                excluded.append({"path": source_key, "reason": "not_legacy_memory"})
                continue

            files, has_credential_assignment = _inventory_files(source)
            inventory_digest = _inventory_sha256(files)
            snapshot_payload = _find_snapshot_payload(
                root,
                source,
                inventory_digest,
                protection_reasons=(
                    ["credential_assignment"] if has_credential_assignment else []
                ),
            )
            if snapshot_payload is None:
                from scripts.loopmem import legacy

                staged = legacy.stage_legacy(root, source)
                snapshot_payload = Path(staged["snapshot_path"])
            now = time.time()
            migration_id = f"m-{uuid.uuid4().hex}"
            source_kind = _source_kind(source, files)
            discovery = discover_project(source.parent)
            catalogued_files, observation_reliable = _observation_snapshot(
                source,
                source_kind,
            )
            if not observation_reliable:
                raise _observation_unknown(source)
            project_id: str | None = None
            if source_kind != "global":
                project_id = registry.resolve_project(discovery, create=True)
                if project_id is None:
                    raise LoopMemoryError(
                        code="corrupt_state",
                        message="Registry did not create a project identity",
                        recoverable=False,
                    )
            if source_kind == "global":
                target = root / "global"
            elif source_kind == "session":
                month = datetime.fromtimestamp(now).strftime("%Y-%m")
                target = (
                    root
                    / "projects"
                    / project_id
                    / "sessions"
                    / "archive"
                    / month
                    / f"s-legacy-{migration_id[2:]}"
                )
            else:
                target = root / "projects" / project_id
            manifest: dict[str, object] = {
                "migration_id": migration_id,
                "schema_version": 2,
                "state": "inventoried",
                "source": source_key,
                "source_kind": source_kind,
                "project_id": project_id,
                "catalogued_files": catalogued_files,
                "files": files,
                "snapshot": snapshot_payload.relative_to(root).as_posix(),
                "source_inventory_sha256": inventory_digest,
                "target": target.resolve(strict=False).relative_to(root).as_posix(),
                "created_at": now,
                "updated_at": now,
                "warnings": [],
            }
            protection_reasons: list[str] = []
            if catalogued_files:
                protection_reasons.append("catalogued_source")
            if has_credential_assignment:
                protection_reasons.append("credential_assignment")
            if protection_reasons:
                manifest["protected"] = True
                manifest["protection_reasons"] = protection_reasons
                manifest["warnings"] = [
                    "Protected legacy source requires explicit approval."
                ]
            manifest_path = assert_loop_path(
                root,
                manifests_dir / f"{migration_id}.json",
            )
            write_json_atomic(manifest_path, manifest)
            _ensure_ledger_event(root, migration_id, "inventoried")
            manifest_paths.append(str(manifest_path))

        return {
            "manifests": manifest_paths,
            "excluded": excluded,
            "warnings": warnings,
        }


def load_manifest(path: Path) -> dict[str, object]:
    path = Path(path)
    manifest = read_json(path)
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, int) and not isinstance(schema_version, bool):
        if schema_version > 2:
            raise LoopMemoryError(
                code="unsupported_schema",
                message=f"Unsupported migration manifest schema: {schema_version}",
                recoverable=False,
            )
        if schema_version == 2 and any(
            isinstance(manifest.get(field), str)
            and Path(manifest[field]).is_absolute()
            for field in ("target", "staging_path", "quarantine_path", "snapshot")
        ):
            raise LoopMemoryError(
                code="unsupported_schema",
                message="Schema 2 manifests must use relative internal paths",
                recoverable=False,
            )
    _validate_manifest(manifest)
    if manifest.get("schema_version") == 2:
        manifest = _inflate_v2_manifest(manifest, path.parent.parent.parent)
    return manifest


def _inflate_v2_manifest(
    manifest: dict[str, object],
    loop_root: Path,
) -> dict[str, object]:
    loop_root = loop_root.resolve(strict=False)
    value = dict(manifest)
    for field in ("target", "staging_path", "quarantine_path"):
        candidate = value.get(field)
        if isinstance(candidate, str) and not Path(candidate).is_absolute():
            value[field] = str(_safe_loop_path(loop_root, loop_root / candidate))
    snapshot = value.get("snapshot")
    if isinstance(snapshot, str) and not Path(snapshot).is_absolute():
        value["snapshot"] = str(_safe_loop_path(loop_root, loop_root / snapshot))
    return value


def _manifest_storage_value(
    manifest: dict[str, object],
    loop_root: Path,
) -> dict[str, object]:
    value = dict(manifest)
    if value.get("schema_version") == 2:
        for field in ("target", "staging_path", "quarantine_path"):
            candidate = value.get(field)
            if isinstance(candidate, str) and Path(candidate).is_absolute():
                try:
                    value[field] = Path(candidate).resolve(strict=False).relative_to(
                        loop_root.resolve(strict=False)
                    ).as_posix()
                except ValueError as error:
                    raise _corrupt_manifest("v2 internal path escapes root") from error
        snapshot = value.get("snapshot")
        if isinstance(snapshot, str) and Path(snapshot).is_absolute():
            try:
                value["snapshot"] = Path(snapshot).resolve(strict=False).relative_to(
                    loop_root.resolve(strict=False)
                ).as_posix()
            except ValueError as error:
                raise _corrupt_manifest("v2 snapshot escapes root") from error
    return value


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    root = path.parent.parent.parent
    write_json_atomic(path, _manifest_storage_value(manifest, root))


def _find_snapshot_payload(
    loop_root: Path,
    source: Path,
    inventory_digest: str,
    *,
    protection_reasons: list[str] | None = None,
) -> Path | None:
    matches = _find_snapshot_payloads(
        loop_root,
        source,
        inventory_digest,
        protection_reasons=protection_reasons,
    )
    return matches[0] if matches else None


def _find_snapshot_payloads(
    loop_root: Path,
    source: Path,
    inventory_digest: str,
    *,
    protection_reasons: list[str] | None = None,
) -> list[Path]:
    snapshots = loop_root / "legacy-snapshots"
    if not snapshots.is_dir():
        return []
    matches: list[Path] = []
    for directory in sorted(snapshots.iterdir(), key=lambda item: item.name):
        if not re.fullmatch(r"l-[0-9a-f]{32}", directory.name):
            continue
        try:
            candidate_manifest = {
                "schema_version": 2,
                "source": str(source),
                "snapshot": (directory / "payload").relative_to(loop_root).as_posix(),
                "files": _inventory_files(directory / "payload")[0],
                "source_inventory_sha256": inventory_digest,
                "protection_reasons": list(protection_reasons or []),
            }
            custody = _manifest_custody_snapshot(loop_root, candidate_manifest)
        except (OSError, LoopMemoryError):
            continue
        if custody.inventory_sha256 == inventory_digest:
            matches.append(custody.path)
    return matches


def _rebind_missing_v2_snapshot(
    loop_root: Path,
    manifest_path: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    """Bind an old v2 manifest to one uniquely verified staged snapshot."""
    try:
        _manifest_custody_snapshot(loop_root, manifest)
        return manifest
    except LoopMemoryError as error:
        if error.code != "legacy_stage_required":
            raise
    if manifest.get("schema_version") != 2:
        raise _legacy_stage_required()
    source = Path(str(manifest["source"]))
    inventory_digest = str(manifest["source_inventory_sha256"])
    matches = _find_snapshot_payloads(
        loop_root,
        source,
        inventory_digest,
        protection_reasons=list(manifest.get("protection_reasons", [])),
    )
    if not matches:
        raise _legacy_stage_required()
    if len(matches) != 1:
        raise LoopMemoryError(
            code="migration_conflict",
            message="Multiple verified legacy-stage snapshots match the manifest",
            recoverable=True,
        )
    root = Path(loop_root).resolve(strict=False)
    safe_manifest_path = _safe_loop_path(root, Path(manifest_path))
    try:
        expected = safe_manifest_path.read_bytes()
    except OSError as error:
        raise _legacy_stage_required() from error
    rebound = dict(manifest)
    rebound["snapshot"] = matches[0].relative_to(root).as_posix()
    rebound["updated_at"] = _next_timestamp(manifest)
    stored = _manifest_storage_value(rebound, root)
    _validate_manifest(stored)
    try:
        written = write_json_atomic_if_unchanged(
            safe_manifest_path,
            stored,
            expected,
        )
    except OSError as error:
        raise _corrupt_manifest("failed to persist custody snapshot binding") from error
    if not written:
        raise LoopMemoryError(
            code="migration_conflict",
            message="Migration manifest changed while restoring custody binding",
            recoverable=True,
        )
    return load_manifest(safe_manifest_path)


def restore_missing_v2_snapshot(
    loop_root: Path,
    manifest_path: Path,
) -> dict[str, object]:
    """Restore one inventoried manifest's missing custody binding under lease."""
    lexical_root = _absolute_lexical(loop_root)
    if is_reserved_product_path(lexical_root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory migrations cannot target product-owned memory",
            recoverable=False,
        )
    root = lexical_root.resolve(strict=False)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory migrations cannot target product-owned memory",
            recoverable=False,
        )
    safe_manifest_path = _safe_loop_path(root, Path(manifest_path))
    lease_path = _safe_loop_path(root, root / "locks" / "migration.lock")
    with FileLease(lease_path, owner="migration-custody-restore"):
        manifest = load_manifest(safe_manifest_path)
        _validate_manifest_location(root, safe_manifest_path, manifest)
        _validate_ledger(root, manifest)
        if manifest.get("state") != "inventoried":
            _manifest_custody_snapshot(root, manifest)
            return manifest
        return _rebind_missing_v2_snapshot(
            root,
            safe_manifest_path,
            manifest,
        )


def is_refresh_metadata_eligible(manifest: dict[str, object]) -> bool:
    return (
        manifest.get("state") == "inventoried"
        and not _REFRESH_FORBIDDEN_FIELDS.intersection(manifest)
    )


def is_refresh_source_compatible(manifest: dict[str, object]) -> bool:
    # Refresh is intentionally custody-only.  External legacy drift is
    # provenance, not migration authority; a new inventory requires a new
    # legacy-stage snapshot rather than rereading the source in place.
    return manifest.get("schema_version") == 2


def _legacy_stage_required() -> LoopMemoryError:
    return LoopMemoryError(
        code="legacy_stage_required",
        message="Legacy migration requires a verified legacy-stage snapshot",
        recoverable=True,
    )


def refresh_migration(
    loop_root: Path,
    manifest_path: Path,
    *,
    namespace_validator: Callable[[], object] | None = None,
) -> dict[str, object]:
    lexical_root = _absolute_lexical(loop_root)
    if is_reserved_product_path(lexical_root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory refresh cannot target product-owned memory",
            recoverable=False,
        )
    root = lexical_root.resolve(strict=False)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory refresh cannot target product-owned memory",
            recoverable=False,
        )
    safe_manifest_path = _safe_loop_path(root, Path(manifest_path))
    lease_path = _safe_loop_path(root, root / "locks" / "migration.lock")

    with FileLease(lease_path, owner="migration-refresh"):
        if namespace_validator is not None:
            namespace_validator()
        manifest = load_manifest(safe_manifest_path)
        try:
            starting_bytes = safe_manifest_path.read_bytes()
            confirmed_manifest = load_manifest(safe_manifest_path)
            confirmed_bytes = safe_manifest_path.read_bytes()
        except (OSError, LoopMemoryError) as error:
            raise _refresh_migration_conflict() from error
        if (
            confirmed_manifest != manifest
            or confirmed_bytes != starting_bytes
        ):
            raise _refresh_migration_conflict()
        _validate_manifest_location(root, safe_manifest_path, manifest)
        _validate_ledger(root, manifest)
        if not is_refresh_metadata_eligible(manifest):
            if manifest["state"] != "inventoried":
                raise _refresh_not_allowed("manifest state is not inventoried")
            raise _refresh_not_allowed(
                "later-transition manifest metadata is present"
            )
        migration_id = manifest["migration_id"]
        artifact_paths = (
            root / "migrations" / "staging" / migration_id,
            root / "migrations" / "quarantine" / migration_id / "source",
            root / "migrations" / "maintenance" / f"{migration_id}.json",
        )
        for artifact_path in artifact_paths:
            _safe_loop_path(root, artifact_path.parent)
            try:
                artifact_path.lstat()
            except FileNotFoundError:
                continue
            except NotADirectoryError as error:
                raise _refresh_not_allowed(
                    "later-transition artifact path is obstructed"
                ) from error
            raise _refresh_not_allowed("later-transition artifact is present")
        if manifest.get("schema_version") != 2:
            raise _legacy_stage_required()
        if RegistryStore(root).resolve_legacy_alias(
            Path(manifest["source"])
        ) is not None:
            raise _refresh_not_allowed("legacy alias is already registered")

        _manifest_custody_snapshot(root, manifest)
        candidate = dict(manifest)
        storage_candidate = _manifest_storage_value(candidate, root)
        try:
            _validate_manifest(storage_candidate)
        except LoopMemoryError as error:
            raise _refresh_source_changed() from error

        try:
            current_bytes = safe_manifest_path.read_bytes()
        except OSError as error:
            raise _refresh_migration_conflict() from error
        if current_bytes != starting_bytes:
            raise _refresh_migration_conflict()
        if storage_candidate == _manifest_storage_value(manifest, root):
            result = dict(manifest)
            digest = _inventory_sha256(manifest["files"])
            result["previous_inventory_sha256"] = digest
            result["current_inventory_sha256"] = digest
            return result

        candidate["updated_at"] = _next_timestamp(manifest)
        try:
            _validate_manifest(_manifest_storage_value(candidate, root))
        except LoopMemoryError as error:
            raise _refresh_source_changed() from error
        try:
            written = write_json_atomic_if_unchanged(
                safe_manifest_path,
                _manifest_storage_value(candidate, root),
                starting_bytes,
            )
        except OSError as error:
            raise _refresh_write_failed() from error
        if not written:
            raise _refresh_migration_conflict()

        result = dict(candidate)
        result["previous_inventory_sha256"] = _inventory_sha256(manifest["files"])
        result["current_inventory_sha256"] = _inventory_sha256(candidate["files"])
        return result


def apply_migration(
    loop_root: Path,
    manifest_path: Path,
    classification_path: Path,
    stop_after: str | None = None,
    *,
    classification_snapshot: ClassificationSnapshot | None = None,
) -> dict[str, object]:
    lexical_root = _absolute_lexical(loop_root)
    if is_reserved_product_path(lexical_root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory migrations cannot target product-owned memory",
            recoverable=False,
        )
    root = lexical_root.resolve(strict=False)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory migrations cannot target product-owned memory",
            recoverable=False,
        )
    if stop_after not in (None, "validated"):
        raise LoopMemoryError(
            code="invalid_stop_after",
            message="Migration stop_after must be validated or None",
        )
    safe_manifest_path = _safe_loop_path(root, Path(manifest_path))
    lease_path = _safe_loop_path(root, root / "locks" / "migration.lock")
    classification_path = Path(classification_path)
    snapshot = classification_snapshot
    if snapshot is not None and not isinstance(snapshot, ClassificationSnapshot):
        raise LoopMemoryError(
            code="invalid_classification",
            message="Migration classification snapshot is invalid",
            recoverable=False,
        )

    while True:
        with FileLease(lease_path, owner="migration"):
            manifest = load_manifest(safe_manifest_path)
            manifest = _rebind_missing_v2_snapshot(
                root,
                safe_manifest_path,
                manifest,
            )
            if snapshot is None:
                snapshot = load_classification_snapshot(
                    classification_path,
                    manifest,
                    root,
                )
            classification, classification_sha256 = _classification_from_snapshot(
                snapshot,
                manifest,
                root,
            )
            _verify_classification_pin(manifest, classification_sha256)
            _validate_manifest_location(root, safe_manifest_path, manifest)
            _manifest_custody_snapshot(root, manifest)
            if manifest.get("schema_version") != 2:
                raise _legacy_stage_required()
            if (
                manifest.get("protected") is True
                and classification.get("approved_protected") is not True
            ):
                raise LoopMemoryError(
                    code="protected_migration",
                    message="Protected legacy source requires explicit approval",
                    recoverable=False,
                )
            if stop_after == "validated":
                if manifest["source_kind"] != "global":
                    raise LoopMemoryError(
                        code="invalid_stop_after",
                        message="validated hold is only valid for global bootstrap",
                        recoverable=False,
                    )
                if _STATE_INDEX[manifest["state"]] > _STATE_INDEX["validated"]:
                    raise LoopMemoryError(
                        code="invalid_stop_after",
                        message="Migration has already advanced past validated",
                        recoverable=False,
                    )
            _validate_ledger(root, manifest)
            _ensure_ledger_event(root, manifest["migration_id"], manifest["state"])
            state = manifest["state"]
            if _STATE_INDEX[state] >= _STATE_INDEX["copied"]:
                _verify_plan_classification(root, manifest, classification)

            with _promotion_leases(root, manifest, classification["actions"]):
                if state == "complete":
                    RegistryStore(root).add_legacy_alias(
                        Path(manifest["source"]),
                        manifest["target"],
                        manifest["migration_id"],
                    )
                    _verify_quarantined_state(root, manifest, require_alias=True)
                    return manifest

                if state == "validated" and stop_after == "validated":
                    _verify_pre_quarantine_state(root, manifest)
                    _verify_target_files(root, manifest)
                    if manifest.get("hold_reason") != "governance_switch":
                        held = dict(manifest)
                        held["hold_reason"] = "governance_switch"
                        held["updated_at"] = _next_timestamp(manifest)
                        _write_manifest(safe_manifest_path, held)
                        return held
                    return manifest

                next_manifest = _advance_transition(
                    root,
                    manifest,
                    classification,
                    classification_sha256,
                    stop_after=stop_after,
                )
                _write_manifest(safe_manifest_path, next_manifest)
                _ensure_ledger_event(
                    root,
                    next_manifest["migration_id"],
                    next_manifest["state"],
                )
                if (
                    next_manifest["state"] == "validated"
                    and stop_after == "validated"
                ):
                    return next_manifest


def recover_migration(
    loop_root: Path,
    manifest_path: Path,
) -> dict[str, object]:
    lexical_root = _absolute_lexical(loop_root)
    if is_reserved_product_path(lexical_root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory recovery cannot target product-owned memory",
            recoverable=False,
        )
    root = lexical_root.resolve(strict=False)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory recovery cannot target product-owned memory",
            recoverable=False,
        )
    safe_manifest_path = _safe_loop_path(root, Path(manifest_path))
    lease_path = _safe_loop_path(root, root / "locks" / "migration.lock")
    with FileLease(lease_path, owner="migration-recovery"):
        manifest = load_manifest(safe_manifest_path)
        _validate_manifest_location(root, safe_manifest_path, manifest)
        _validate_ledger(root, manifest)
        if manifest.get("schema_version") == 1:
            if _real_directory_exists(_quarantine_source(root, manifest)):
                pass
            else:
                raise _legacy_stage_required()
        else:
            _manifest_custody_snapshot(root, manifest)
        state = manifest["state"]
        if state == "detected":
            if manifest.get("schema_version") == 2:
                _manifest_custody_snapshot(root, manifest)
            else:
                _verify_detected_state(root, manifest)
        elif state in ("inventoried", "copied", "validated"):
            _verify_pre_quarantine_state(root, manifest)
            if _STATE_INDEX[state] >= _STATE_INDEX["copied"]:
                _verify_target_files(root, manifest)
        elif state == "references_updated":
            _verify_references_updated_state(root, manifest)
        elif state == "quarantined":
            _verify_quarantined_state(root, manifest, require_alias=False)
            _ensure_ledger_event(
                root,
                manifest["migration_id"],
                manifest["state"],
            )
            RegistryStore(root).add_legacy_alias(
                Path(manifest["source"]),
                manifest["target"],
                manifest["migration_id"],
            )
            completed = dict(manifest)
            completed["state"] = "complete"
            completed["updated_at"] = _next_timestamp(manifest)
            _write_manifest(safe_manifest_path, completed)
            _ensure_ledger_event(root, completed["migration_id"], "complete")
            result = dict(completed)
            result["recovery"] = "completed_quarantine"
            return result
        elif state == "complete":
            RegistryStore(root).add_legacy_alias(
                Path(manifest["source"]),
                manifest["target"],
                manifest["migration_id"],
            )
            _verify_quarantined_state(root, manifest, require_alias=True)
        else:
            raise _corrupt_manifest(f"cannot recover state {state!r}")
        repaired = _ensure_ledger_event(
            root,
            manifest["migration_id"],
            manifest["state"],
        )
        result = dict(manifest)
        result["recovery"] = "ledger_repaired" if repaired else "consistent"
        return result


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _containing_memory_root(candidate: Path) -> Path | None:
    absolute = _absolute_lexical(candidate)
    indexes = [index for index, part in enumerate(absolute.parts) if part == ".memory"]
    if not indexes:
        return None
    return Path(*absolute.parts[: indexes[-1] + 1])


def _reject_loop_source_overlap(
    loop_root: Path,
    candidates: list[Path],
) -> None:
    for candidate in candidates:
        source = _containing_memory_root(candidate)
        if source is None:
            continue
        normalized = source.resolve(strict=False)
        if _is_relative_to(normalized, loop_root) or _is_relative_to(
            loop_root,
            normalized,
        ):
            raise LoopMemoryError(
                code="unsafe_legacy_source",
                message=(
                    "Loop root and legacy source must not overlap: "
                    f"{loop_root} and {normalized}"
                ),
                recoverable=False,
            )


def _contains_symlink(source: Path) -> bool:
    source_stat = _source_root_stat(source, missing_ok=True)
    if source_stat is None:
        return False
    if stat.S_ISLNK(source_stat.st_mode):
        return True
    if not stat.S_ISDIR(source_stat.st_mode):
        return False
    try:
        with closing(
            _descriptor_entries(source, source_stat, validate_paths=False)
        ) as entries:
            for _ in entries:
                pass
        return False
    except LoopMemoryError as error:
        if error.code == "unsafe_legacy_source":
            return True
        raise


def _source_unstable() -> LoopMemoryError:
    return LoopMemoryError(
        code="source_unstable",
        message="Legacy source changed or could not be read consistently",
        recoverable=True,
    )


def _unsafe_legacy_source(message: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="unsafe_legacy_source",
        message=message,
        recoverable=False,
    )


def _source_root_stat(
    source: Path,
    *,
    missing_ok: bool,
) -> os.stat_result | None:
    try:
        return source.lstat()
    except FileNotFoundError as error:
        if missing_ok:
            return None
        raise _source_unstable() from error
    except OSError as error:
        raise _source_unstable() from error


def _descriptor_flags() -> tuple[int, int]:
    directory = getattr(os, "O_DIRECTORY", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if (
        not isinstance(directory, int)
        or not isinstance(no_follow, int)
        or directory == 0
        or no_follow == 0
        or not _SCANDIR_FD_SUPPORTED
        or not _OPEN_DIR_FD_SUPPORTED
        or not _STAT_DIR_FD_SUPPORTED
        or _NOFOLLOW_STAT_FUNCTION
        not in getattr(os, "supports_follow_symlinks", ())
    ):
        raise _unsafe_legacy_source(
            "Legacy source cannot be inventoried without no-follow traversal"
        )
    common = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    return common | directory, common | getattr(os, "O_NONBLOCK", 0)


class _ManagedDescriptor:
    def __init__(self, descriptor: int):
        self.descriptor = descriptor

    def __enter__(self) -> int:
        return self.descriptor

    def __exit__(self, error_type, error, traceback) -> bool:
        try:
            os.close(self.descriptor)
        except OSError as close_error:
            if error_type is None:
                raise _source_unstable() from close_error
        return False


def _open_descriptor(
    path: Path | str,
    flags: int,
    *,
    dir_fd: int | None = None,
) -> _ManagedDescriptor:
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _unsafe_legacy_source(
                "Legacy source contains a symlink"
            ) from error
        if error.errno == errno.ENOTDIR:
            try:
                live_stat = os.stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except (OSError, NotImplementedError) as stat_error:
                raise _source_unstable() from stat_error
            _verify_open_directory_binding(None, live_stat)
        raise _source_unstable() from error
    return _ManagedDescriptor(descriptor)


def _node_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _full_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verify_current_user_owner(value: os.stat_result) -> None:
    if value.st_uid != os.getuid():
        raise _unsafe_legacy_source(
            "Loop custody is not owned by the current user"
        )


def _verify_open_directory_binding(
    opened_stat: os.stat_result | None,
    live_stat: os.stat_result,
) -> None:
    if stat.S_ISLNK(live_stat.st_mode) or not (
        stat.S_ISDIR(live_stat.st_mode) or stat.S_ISREG(live_stat.st_mode)
    ):
        raise _unsafe_legacy_source(
            "Legacy source contains a symlink or special file"
        )
    if opened_stat is None or not stat.S_ISDIR(
        live_stat.st_mode
    ) or _full_identity(
        live_stat
    ) != _full_identity(opened_stat):
        raise _source_unstable()


def _checked_fstat(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError as error:
        raise _source_unstable() from error


def _descriptor_entries(
    source: Path,
    source_stat: os.stat_result,
    *,
    validate_paths: bool,
):
    directory_flags, _ = _descriptor_flags()
    with _open_descriptor(source, directory_flags) as root_descriptor:
        opened_stat = _checked_fstat(root_descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise _unsafe_legacy_source(
                "Legacy source contains a special file"
            )
        if _node_identity(opened_stat) != _node_identity(source_stat):
            raise _source_unstable()
        yield from _directory_entries(
            root_descriptor,
            (),
            directory_flags,
            validate_paths=validate_paths,
        )
        live_stat = _source_root_stat(source, missing_ok=False)
        if live_stat is None:
            raise _source_unstable()
        _verify_open_directory_binding(opened_stat, live_stat)


def _directory_entries(
    directory_descriptor: int,
    parent_parts: tuple[str, ...],
    directory_flags: int,
    *,
    validate_paths: bool,
):
    try:
        entries = os.scandir(directory_descriptor)
        with entries:
            for entry in entries:
                name = entry.name
                relative_path = PurePosixPath(*parent_parts, name).as_posix()
                if validate_paths:
                    try:
                        _validate_relative_path(relative_path, "legacy source")
                    except LoopMemoryError as error:
                        raise _unsafe_legacy_source(
                            "Legacy source path is unsafe"
                        ) from error
                entry_stat = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise _unsafe_legacy_source(
                        "Legacy source contains a symlink"
                    )
                if not stat.S_ISDIR(entry_stat.st_mode):
                    yield (
                        directory_descriptor,
                        name,
                        relative_path,
                        entry_stat,
                    )
                    continue
                with _open_descriptor(
                    name,
                    directory_flags,
                    dir_fd=directory_descriptor,
                ) as child_descriptor:
                    opened_stat = _checked_fstat(child_descriptor)
                    if not stat.S_ISDIR(opened_stat.st_mode):
                        raise _unsafe_legacy_source(
                            "Legacy source contains a special file"
                        )
                    if _node_identity(opened_stat) != _node_identity(entry_stat):
                        raise _source_unstable()
                    yield from _directory_entries(
                        child_descriptor,
                        (*parent_parts, name),
                        directory_flags,
                        validate_paths=validate_paths,
                    )
                    live_stat = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    _verify_open_directory_binding(opened_stat, live_stat)
    except LoopMemoryError:
        raise
    except OSError as error:
        raise _source_unstable() from error


def _read_inventory_file(
    directory_descriptor: int,
    name: str,
    expected_stat: os.stat_result,
) -> bytes:
    _, file_flags = _descriptor_flags()
    with _open_descriptor(
        name,
        file_flags,
        dir_fd=directory_descriptor,
    ) as descriptor:
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise _unsafe_legacy_source(
                    "Legacy source contains a special file"
                )
            expected_identity = _full_identity(expected_stat)
            if _full_identity(opened_stat) != expected_identity:
                raise _source_unstable()

            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)

            content = b"".join(chunks)
            descriptor_stat = os.fstat(descriptor)
            path_stat = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(path_stat.st_mode):
                raise _unsafe_legacy_source(
                    "Legacy source contains a symlink"
                )
            if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(
                path_stat.st_mode
            ):
                raise _unsafe_legacy_source(
                    "Legacy source contains a special file"
                )
            if not (
                _full_identity(descriptor_stat)
                == expected_identity
                == _full_identity(path_stat)
            ) or len(content) != opened_stat.st_size:
                raise _source_unstable()
            return content
        except OSError as error:
            raise _source_unstable() from error


def _inventory_files(source: Path) -> tuple[list[dict[str, object]], bool]:
    files: list[dict[str, object]] = []
    credential_assignment = False
    source_stat = _source_root_stat(source, missing_ok=False)
    if source_stat is None:
        raise _source_unstable()
    if stat.S_ISLNK(source_stat.st_mode):
        raise _unsafe_legacy_source("Legacy source contains a symlink")
    if not stat.S_ISDIR(source_stat.st_mode):
        raise _unsafe_legacy_source("Legacy source contains a special file")
    with closing(
        _descriptor_entries(source, source_stat, validate_paths=True)
    ) as entries:
        for directory_descriptor, name, relative_path, entry_stat in entries:
            if not stat.S_ISREG(entry_stat.st_mode):
                raise _unsafe_legacy_source(
                    "Legacy source contains a special file"
                )
            content = _read_inventory_file(
                directory_descriptor,
                name,
                entry_stat,
            )
            files.append(
                {
                    "relative_path": relative_path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
            credential_assignment = credential_assignment or bool(
                _CREDENTIAL_ASSIGNMENT.search(content)
            )
    files.sort(key=lambda record: str(record["relative_path"]))
    return files, credential_assignment


def _canonical_legacy_global_root() -> Path:
    return _absolute_lexical(Path.home() / ".codex" / ".memory")


def _source_kind(
    source: Path,
    files: list[dict[str, object]],
) -> str:
    if not files:
        return "empty"
    relative_paths = [record["relative_path"] for record in files]
    if _absolute_lexical(source) == _canonical_legacy_global_root() and any(
        name in relative_paths for name in ("long.md", "medium.md", "short.md")
    ):
        return "global"
    for relative_path in relative_paths:
        parts = Path(relative_path).parts
        if (
            len(parts) >= 2
            and parts[0] == "agents"
            and parts[-1] in ("status.md", "handoff.md")
        ):
            return "session"
    return "project"


def _observation_snapshot(
    source: Path,
    source_kind: str,
) -> tuple[list[str], bool]:
    # Loop Memory identity is directory-based and intentionally opaque to
    # repository metadata.  No external command or index inspection belongs
    # in a source inventory; credentials remain the only local protection.
    return [], True


def _stable_source_snapshot(source: Path, source_kind: str) -> SourceSnapshot:
    snapshots: list[SourceSnapshot] = []
    observation_reliability: list[bool] = []
    for _ in range(2):
        files, has_credential_assignment = _inventory_files(source)
        catalogued_files, reliable = _observation_snapshot(source, source_kind)
        snapshots.append(
            SourceSnapshot(
                files=[dict(record) for record in files],
                catalogued_files=list(catalogued_files),
                has_credential_assignment=has_credential_assignment,
            )
        )
        observation_reliability.append(reliable)
    if not all(observation_reliability):
        raise _observation_unknown(source)
    if snapshots[0] != snapshots[1]:
        raise _source_unstable()
    return snapshots[0]


def _refresh_source_snapshot(manifest: dict[str, object]) -> SourceSnapshot:
    source = Path(manifest["source"])
    source_stat = _source_root_stat(source, missing_ok=False)
    if source_stat is None:
        raise _source_unstable()
    source_mode = source_stat.st_mode
    if stat.S_ISLNK(source_mode) or not stat.S_ISDIR(source_mode):
        raise _unsafe_legacy_source(
            "Legacy source contains a symlink or special file"
        )
    snapshot = _stable_source_snapshot(source, manifest["source_kind"])
    inventory_paths = {
        record["relative_path"] for record in snapshot.files
    }
    if (
        _source_kind(source, snapshot.files) != manifest["source_kind"]
        or not set(snapshot.catalogued_files).issubset(inventory_paths)
    ):
        raise _refresh_source_changed()
    return snapshot


def _refresh_source_protection(
    manifest: dict[str, object],
) -> dict[str, object]:
    if manifest["source_kind"] == "global" or _STATE_INDEX[
        manifest["state"]
    ] >= _STATE_INDEX["quarantined"]:
        return manifest
    source = Path(manifest["source"])
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        return manifest
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        return manifest
    catalogued_files, reliable = _observation_snapshot(source, manifest["source_kind"])
    if not reliable:
        raise _observation_unknown(source)
    previous = set(manifest["catalogued_files"])
    current = set(catalogued_files)
    additions = current - previous
    if not additions:
        return manifest
    inventory = {record["relative_path"] for record in manifest["files"]}
    if not additions.issubset(inventory):
        raise LoopMemoryError(
            code="source_changed",
            message=f"New catalogued files are outside the migration inventory: {source}",
            recoverable=False,
        )
    updated = dict(manifest)
    updated["catalogued_files"] = sorted(previous | current)
    updated["protected"] = True
    reasons = list(updated.get("protection_reasons", []))
    if "catalogued_source" not in reasons:
        reasons.append("catalogued_source")
    updated["protection_reasons"] = reasons
    warnings = list(updated["warnings"])
    warning = "Protected legacy source requires explicit approval."
    if warning not in warnings:
        warnings.append(warning)
    updated["warnings"] = warnings
    updated["updated_at"] = _next_timestamp(manifest)
    return updated


def _observation_unknown(source: Path) -> LoopMemoryError:
    return LoopMemoryError(
        code="observation_unknown",
        message=f"Repository observation could not be determined safely: {source}",
        recoverable=False,
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _advance_transition(
    loop_root: Path,
    manifest: dict[str, object],
    classification: dict[str, object],
    classification_sha256: str,
    *,
    stop_after: str | None,
) -> dict[str, object]:
    state = manifest["state"]
    updated = dict(manifest)
    if state == "detected":
        if manifest.get("schema_version") == 2:
            _manifest_custody_snapshot(loop_root, manifest)
        else:
            updated = _reinventoried_manifest(loop_root, manifest)
        updated["state"] = "inventoried"
    elif state == "inventoried":
        _verify_pre_quarantine_state(loop_root, manifest)
        staged = _stage_actions(
            loop_root,
            manifest,
            classification,
            classification_sha256,
        )
        updated.update(staged)
        updated["classification_sha256"] = classification_sha256
        updated["state"] = "copied"
    elif state == "copied":
        _verify_pre_quarantine_state(loop_root, manifest)
        _publish_staged(loop_root, manifest)
        _ensure_canonical_target_layout(loop_root, manifest)
        updated["state"] = "validated"
        if stop_after == "validated":
            updated["hold_reason"] = "governance_switch"
    elif state == "validated":
        _verify_pre_quarantine_state(loop_root, manifest)
        _verify_target_files(loop_root, manifest)
        updated.pop("hold_reason", None)
        updated["state"] = "references_updated"
    elif state == "references_updated":
        _verify_target_files(loop_root, manifest)
        if manifest.get("schema_version") == 2:
            snapshot_source = _manifest_snapshot_root(loop_root, manifest)
            _verify_inventory(snapshot_source, manifest["files"], "corrupt_state")
            RegistryStore(loop_root).add_legacy_alias(
                Path(manifest["source"]), manifest["target"], manifest["migration_id"]
            )
            updated["quarantine_path"] = snapshot_source.relative_to(loop_root).as_posix()
            updated["state"] = "quarantined"
            updated["updated_at"] = _next_timestamp(manifest)
            return updated
        quarantine_source = _quarantine_source(loop_root, manifest)
        source = Path(manifest["source"])
        source_exists = _real_directory_exists(source)
        quarantine_exists = _real_directory_exists(quarantine_source)
        if source_exists and quarantine_exists:
            raise _corrupt_manifest("source and quarantine both exist")
        if source_exists:
            _verify_inventory(source, manifest["files"], "source_changed")
            raise LoopMemoryError(
                code="legacy_source_retained",
                message="External legacy source remains read-only; use its verified snapshot",
                recoverable=False,
            )
        elif quarantine_exists:
            _verify_inventory(
                quarantine_source,
                manifest["files"],
                "corrupt_state",
            )
        else:
            raise _corrupt_manifest("source and quarantine are both missing")
        RegistryStore(loop_root).add_legacy_alias(
            Path(manifest["source"]), manifest["target"], manifest["migration_id"]
        )
        updated["quarantine_path"] = str(quarantine_source)
        updated["state"] = "quarantined"
    elif state == "quarantined":
        _verify_quarantined_state(loop_root, manifest, require_alias=True)
        updated["state"] = "complete"
    else:
        raise _corrupt_manifest(f"cannot advance state {state!r}")
    updated["updated_at"] = _next_timestamp(manifest)
    return updated


def _reinventoried_manifest(
    loop_root: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    source = Path(manifest["source"])
    if not _real_directory_exists(source) or _contains_symlink(source):
        raise LoopMemoryError(
            code="unsafe_legacy_source",
            message=f"Detected legacy source is not a real safe directory: {source}",
            recoverable=False,
        )
    files, has_credential_assignment = _inventory_files(source)
    source_kind = _source_kind(source, files)
    updated = dict(manifest)
    updated["files"] = files
    updated["source_kind"] = source_kind
    discovery = discover_project(source.parent)
    catalogued_files, observation_reliable = _observation_snapshot(source, source_kind)
    if not observation_reliable:
        raise _observation_unknown(source)
    updated["catalogued_files"] = catalogued_files
    project_id: str | None = None
    if source_kind != "global":
        store = RegistryStore(loop_root)
        store.initialize()
        project_id = store.resolve_project(discovery, create=True)
    updated["project_id"] = project_id
    if source_kind == "global":
        target = loop_root / "global"
    elif source_kind == "session":
        month = datetime.fromtimestamp(manifest["created_at"]).strftime("%Y-%m")
        target = (
            loop_root
            / "projects"
            / project_id
            / "sessions"
            / "archive"
            / month
            / f"s-legacy-{manifest['migration_id'][2:]}"
        )
    else:
        target = loop_root / "projects" / project_id
    safe_target = _safe_loop_path(loop_root, target)
    updated["target"] = (
        safe_target.relative_to(loop_root).as_posix()
        if manifest.get("schema_version") == 2
        else str(safe_target)
    )
    reasons: list[str] = []
    if catalogued_files:
        reasons.append("catalogued_source")
    if has_credential_assignment:
        reasons.append("credential_assignment")
    if reasons:
        updated["protected"] = True
        updated["protection_reasons"] = reasons
        updated["warnings"] = [
            "Protected legacy source requires explicit approval."
        ]
    else:
        updated.pop("protected", None)
        updated.pop("protection_reasons", None)
    return updated


def _stage_actions(
    loop_root: Path,
    manifest: dict[str, object],
    classification: dict[str, object],
    classification_sha256: str,
) -> dict[str, object]:
    source_root = _manifest_snapshot_root(loop_root, manifest)
    prepared: list[tuple[dict[str, object], bytes | None]] = []
    actions = list(classification["actions"])
    project_merge_actions = [
        action
        for action in actions
        if manifest["source_kind"] == "project"
        and action["destination"] == "project/project.md"
        and action["mode"] == "merge_entries"
    ]
    if project_merge_actions:
        prepared.append(
            _prepare_project_merge_action(
                loop_root,
                manifest,
                project_merge_actions,
            )
        )
        actions = [action for action in actions if action not in project_merge_actions]
    for action in actions:
        if action["mode"] == "discard_empty":
            continue
        if action["mode"] == "quarantine_only":
            prepared.append(
                (
                    {
                        "source": action["source"],
                        "destination": action["destination"],
                        "mode": action["mode"],
                    },
                    None,
                )
            )
            continue
        source = source_root.joinpath(*PurePosixPath(action["source"]).parts)
        source_content = _read_inventoried_source_bytes(
            loop_root, manifest, action["source"]
        )
        destination = _classification_destination(
            loop_root,
            manifest,
            action["destination"],
        )
        baseline, baseline_content = _baseline_snapshot(destination)
        if action["mode"] == "copy":
            candidate_content = source_content
            source_entry_hashes: list[str] = []
            candidate_entry_hashes: list[str] = []
            if action["destination"] == "project/project.md":
                _validate_project_document(candidate_content, source)
            if baseline["exists"] and baseline_content != candidate_content:
                raise LoopMemoryError(
                    code="migration_conflict",
                    message=f"Migration target has different content: {destination}",
                    recoverable=False,
                )
        else:
            (
                candidate_content,
                source_entry_hashes,
                candidate_entry_hashes,
            ) = _merge_entries_candidate(
                source,
                source_content,
                baseline_content,
                action["destination"],
                destination,
            )
        target_relative_path = destination.relative_to(loop_root).as_posix()
        candidate_relative_path = f"candidates/{target_relative_path}"
        candidate = {
            "relative_path": candidate_relative_path,
            "sha256": hashlib.sha256(candidate_content).hexdigest(),
            "size": len(candidate_content),
        }
        plan_action: dict[str, object] = {
            "source": action["source"],
            "destination": action["destination"],
            "mode": action["mode"],
            "target_relative_path": target_relative_path,
            "baseline": baseline,
            "candidate": candidate,
            "source_entry_sha256": source_entry_hashes,
            "candidate_entry_sha256": candidate_entry_hashes,
        }
        prepared.append((plan_action, candidate_content))

    staging = _staging_path(loop_root, manifest)
    _reset_staging(staging)
    ensure_directory(staging)
    for plan_action, candidate_content in prepared:
        if candidate_content is None:
            continue
        candidate_path = _safe_staging_path(
            staging,
            plan_action["candidate"]["relative_path"],
        )
        _write_bytes_atomic_replace(candidate_path, candidate_content)
    plan: dict[str, object] = {
        "migration_id": manifest["migration_id"],
        "schema_version": 1,
        "classification_sha256": classification_sha256,
        "actions": [plan_action for plan_action, _ in prepared],
    }
    plan_bytes = _json_bytes(plan)
    plan_path = _safe_staging_path(staging, "publish-plan.json")
    _write_bytes_atomic_replace(plan_path, plan_bytes)
    target_files = [
        {
            "relative_path": plan_action["target_relative_path"],
            "sha256": plan_action["candidate"]["sha256"],
            "size": plan_action["candidate"]["size"],
        }
        for plan_action, candidate_content in prepared
        if candidate_content is not None
    ]
    target_files.sort(key=lambda record: record["relative_path"])
    return {
        "target_files": target_files,
        "staging_path": str(staging),
        "publish_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
    }


def _read_inventoried_source_bytes(
    loop_root: Path,
    manifest: dict[str, object],
    relative_path: str,
) -> bytes:
    inventory = {
        record["relative_path"]: record
        for record in manifest["files"]
    }
    record = inventory[relative_path]
    custody = _manifest_custody_snapshot(loop_root, manifest)
    expected_identities = dict(custody.node_identities)
    expected_identity = expected_identities.get(relative_path)
    if expected_identity is None:
        raise _corrupt_manifest("migration source identity is missing")
    source = custody.path.joinpath(*PurePosixPath(relative_path).parts)
    try:
        current_root = _custody_node_identities(custody.path)
        if current_root != custody.node_identities:
            raise _corrupt_manifest("custody snapshot changed before source read")
        directory_flags, file_flags = _descriptor_flags()
        with _open_descriptor(custody.path, directory_flags) as directory_descriptor:
            root_opened = _checked_fstat(directory_descriptor)
            if _full_identity(root_opened) != custody.snapshot_identity:
                raise _corrupt_manifest("custody snapshot root changed before source read")
            parts = PurePosixPath(relative_path).parts
            with ExitStack() as stack:
                parent_descriptor = directory_descriptor
                for index, part in enumerate(parts[:-1]):
                    child = stack.enter_context(
                        _open_descriptor(part, directory_flags, dir_fd=parent_descriptor)
                    )
                    expected_directory = expected_identities.get(
                        PurePosixPath(*parts[: index + 1]).as_posix()
                    )
                    if expected_directory is None or _full_identity(
                        _checked_fstat(child)
                    ) != expected_directory:
                        raise _corrupt_manifest("custody source parent identity changed")
                    parent_descriptor = child
                with _open_descriptor(
                    parts[-1],
                    file_flags,
                    dir_fd=parent_descriptor,
                ) as descriptor:
                    opened = _checked_fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode) or _full_identity(opened) != expected_identity:
                        raise _corrupt_manifest("custody source identity changed")
                    chunks = []
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if _full_identity(_checked_fstat(descriptor)) != expected_identity:
                        raise _corrupt_manifest("custody source changed during read")
        final_binding = _manifest_custody_snapshot(loop_root, manifest)
        if (
            final_binding.node_identities != custody.node_identities
            or final_binding.receipt_identity != custody.receipt_identity
            or final_binding.receipt_sha256 != custody.receipt_sha256
        ):
            raise _corrupt_manifest("custody snapshot changed during source read")
    except (FileNotFoundError, LoopMemoryError, OSError) as error:
        if isinstance(error, LoopMemoryError) and error.code == "corrupt_state":
            raise
        raise LoopMemoryError(
            code="source_changed",
            message=f"Migration source changed during staging: {source}",
            recoverable=False,
        ) from error
    if (
        len(content) != record["size"]
        or hashlib.sha256(content).hexdigest() != record["sha256"]
    ):
        raise LoopMemoryError(
            code="source_changed",
            message=f"Migration source changed during staging: {source}",
            recoverable=False,
        )
    return content


def _prepare_project_merge_action(
    loop_root: Path,
    manifest: dict[str, object],
    actions: list[dict[str, object]],
) -> tuple[dict[str, object], bytes]:
    destination = _classification_destination(
        loop_root,
        manifest,
        "project/project.md",
    )
    baseline, baseline_content = _baseline_snapshot(destination)
    if baseline_content is None:
        candidate_text = _project_memory_template()
    else:
        try:
            candidate_text = baseline_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LoopMemoryError(
                code="migration_conflict",
                message=f"Project target is not UTF-8: {destination}",
                recoverable=False,
            ) from error
    _project_section(candidate_text, PROJECT_SECTIONS[0], destination)

    source_root = _manifest_snapshot_root(loop_root, manifest)
    sources: list[dict[str, str]] = []
    source_hash_sets = {section: set() for section in PROJECT_SECTIONS}
    for action in actions:
        source = source_root.joinpath(*PurePosixPath(action["source"]).parts)
        source_content = _read_inventoried_source_bytes(
            loop_root,
            manifest,
            action["source"],
        )
        try:
            source_text = source_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LoopMemoryError(
                code="migration_conflict",
                message=f"Entry source is not UTF-8: {source}",
                recoverable=False,
            ) from error
        source_blocks = _entry_blocks(source_text, source)
        section = action["section"]
        source_hash_sets[section].update(_entry_hashes(source_blocks))
        sources.append({"source": action["source"], "section": section})
        _, section_end, destination_blocks = _project_section(
            candidate_text,
            section,
            destination,
        )
        candidate_text = _merge_blocks_into_section(
            candidate_text,
            section_end,
            destination_blocks,
            source_blocks,
        )

    candidate_content = candidate_text.encode("utf-8")
    target_relative_path = destination.relative_to(loop_root).as_posix()
    candidate = {
        "relative_path": f"candidates/{target_relative_path}",
        "sha256": hashlib.sha256(candidate_content).hexdigest(),
        "size": len(candidate_content),
    }
    plan_action: dict[str, object] = {
        "sources": sources,
        "destination": "project/project.md",
        "mode": "merge_entries",
        "target_relative_path": target_relative_path,
        "baseline": baseline,
        "candidate": candidate,
        "source_entry_sha256_by_section": {
            section: sorted(source_hash_sets[section])
            for section in PROJECT_SECTIONS
        },
        "candidate_entry_sha256_by_section": _project_entry_hash_mapping(
            candidate_text,
            destination,
        ),
    }
    return plan_action, candidate_content


def _promotion_leases(
    loop_root: Path,
    manifest: dict[str, object],
    actions: list[dict[str, object]],
) -> ExitStack:
    lease_names: set[str] = set()
    if manifest["source_kind"] == "global":
        for action in actions:
            destination = action["destination"]
            if destination.startswith("global/"):
                horizon = PurePosixPath(destination).stem
                lease_names.add(f"promote-global-{horizon}.lock")
    elif manifest["source_kind"] == "project" and actions:
        lease_names.add(f"promote-project-{manifest['project_id']}.lock")

    stack = ExitStack()
    try:
        for lease_name in sorted(lease_names):
            lease_path = _safe_loop_path(
                loop_root,
                loop_root / "locks" / lease_name,
            )
            stack.enter_context(
                FileLease(
                    lease_path,
                    owner=f"migration-publish:{manifest['migration_id']}",
                )
            )
    except BaseException:
        stack.close()
        raise
    return stack


def _staging_path(
    loop_root: Path,
    manifest: dict[str, object],
) -> Path:
    return _safe_loop_path(
        loop_root,
        loop_root / "migrations" / "staging" / manifest["migration_id"],
    )


def _reset_staging(staging: Path) -> None:
    try:
        mode = staging.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or _contains_symlink(staging):
        raise LoopMemoryError(
            code="unsafe_path",
            message=f"Migration staging is unsafe: {staging}",
            recoverable=False,
        )
    for directory, directories, files in os.walk(staging, topdown=False):
        current = Path(directory)
        for name in files:
            path = current / name
            item_mode = path.lstat().st_mode
            if not stat.S_ISREG(item_mode):
                raise LoopMemoryError(
                    code="unsafe_path",
                    message=f"Migration staging contains an unsafe entry: {path}",
                    recoverable=False,
                )
            path.unlink()
        for name in directories:
            (current / name).rmdir()
    staging.rmdir()
    _fsync_directory(staging.parent)


def _safe_staging_path(staging: Path, relative_path: str) -> Path:
    _validate_relative_path(relative_path, "staging")
    lexical = Path(
        os.path.abspath(
            staging.joinpath(*PurePosixPath(relative_path).parts)
        )
    )
    if not _is_relative_to(lexical, staging):
        raise _corrupt_manifest("staging path escapes the migration staging root")
    if lexical.resolve(strict=False) != lexical:
        raise LoopMemoryError(
            code="unsafe_path",
            message=f"Migration staging path traverses a symlink: {lexical}",
            recoverable=False,
        )
    return lexical


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _write_bytes_atomic_replace(path: Path, content: bytes) -> None:
    ensure_directory(path.parent)
    if path.exists() or path.is_symlink():
        _assert_regular_file(path, "migration publish target")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    identity: tuple[int, int] | None = None
    try:
        stream = temporary.open("xb")
        with stream:
            try:
                identity = _path_identity_from_descriptor(stream.fileno())
            except BaseException:
                identity = _path_identity_from_open_descriptor(stream.fileno())
                raise
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        identity = None
        _fsync_directory(path.parent)
    finally:
        if identity is not None:
            _unlink_path_if_identity(temporary, identity)


def _baseline_snapshot(path: Path) -> tuple[dict[str, object], bytes | None]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "sha256": None, "size": None}, None
    _assert_regular_file(path, "migration target")
    content = path.read_bytes()
    return (
        {
            "exists": True,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        },
        content,
    )


def _load_publish_plan(
    loop_root: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    staging = _staging_path(loop_root, manifest)
    if manifest.get("staging_path") != str(staging):
        raise _corrupt_manifest("staging_path does not match migration_id")
    try:
        staging_mode = staging.lstat().st_mode
    except FileNotFoundError as error:
        raise _corrupt_manifest("migration staging is missing") from error
    if stat.S_ISLNK(staging_mode) or not stat.S_ISDIR(staging_mode):
        raise _corrupt_manifest("migration staging is not a real directory")
    if _contains_symlink(staging):
        raise LoopMemoryError(
            code="unsafe_path",
            message=f"Migration staging contains a symlink: {staging}",
            recoverable=False,
        )

    plan_path = _safe_staging_path(staging, "publish-plan.json")
    try:
        _assert_regular_file(plan_path, "migration publish plan")
        content = plan_path.read_bytes()
    except FileNotFoundError as error:
        raise _corrupt_manifest("migration publish plan is missing") from error
    if hashlib.sha256(content).hexdigest() != manifest.get("publish_plan_sha256"):
        raise _corrupt_manifest("migration publish plan hash changed")
    plan = _parse_publish_plan_bytes(content, plan_path)
    if set(plan) != {
        "migration_id",
        "schema_version",
        "classification_sha256",
        "actions",
    }:
        raise _corrupt_manifest("publish plan fields are invalid")
    if plan["migration_id"] != manifest["migration_id"]:
        raise _corrupt_manifest("publish plan migration_id does not match")
    if plan["schema_version"] != 1 or isinstance(plan["schema_version"], bool):
        raise _corrupt_manifest("publish plan schema_version is invalid")
    if plan["classification_sha256"] != manifest.get("classification_sha256"):
        raise _corrupt_manifest("publish plan classification hash does not match")
    actions = plan["actions"]
    if not isinstance(actions, list):
        raise _corrupt_manifest("publish plan actions must be a list")

    inventory_paths = {
        record["relative_path"] for record in manifest["files"]
    }
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    seen_targets: set[str] = set()
    expected_staging_files = {"publish-plan.json"}
    expected_target_files: list[dict[str, object]] = []
    for action in actions:
        _validate_publish_plan_action(
            loop_root,
            manifest,
            staging,
            action,
            inventory_paths,
        )
        if action["mode"] == "quarantine_only":
            source = action["source"]
            if source in seen_sources:
                raise _corrupt_manifest("publish plan contains duplicate sources")
            seen_sources.add(source)
            continue
        destination = action["destination"]
        target_relative_path = action["target_relative_path"]
        for source in _plan_action_sources(action):
            if source in seen_sources:
                raise _corrupt_manifest("publish plan contains duplicate sources")
            seen_sources.add(source)
        if destination in seen_destinations:
            raise _corrupt_manifest("publish plan contains duplicate destinations")
        if target_relative_path in seen_targets:
            raise _corrupt_manifest("publish plan contains duplicate targets")
        seen_destinations.add(destination)
        seen_targets.add(target_relative_path)
        candidate = action["candidate"]
        expected_staging_files.add(candidate["relative_path"])
        expected_target_files.append(
            {
                "relative_path": target_relative_path,
                "sha256": candidate["sha256"],
                "size": candidate["size"],
            }
        )
    _validate_global_readme_authority(manifest, actions, _corrupt_manifest)
    if seen_sources != inventory_paths:
        raise _corrupt_manifest("publish plan does not cover the source inventory")
    expected_target_files.sort(key=lambda record: record["relative_path"])
    if manifest.get("target_files") != expected_target_files:
        raise _corrupt_manifest("publish plan does not match target_files")
    _validate_staging_tree(staging, expected_staging_files)
    return plan


def _plan_action_sources(action: dict[str, object]) -> list[str]:
    if "sources" in action:
        return [record["source"] for record in action["sources"]]
    return [action["source"]]


def _parse_publish_plan_bytes(content: bytes, path: Path) -> dict[str, object]:
    def reject_duplicate_keys(pairs):
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError(f"non-finite constant: {value}")

    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise _corrupt_manifest(f"publish plan JSON is invalid: {path}") from error
    if not isinstance(parsed, dict):
        raise _corrupt_manifest("publish plan JSON must be an object")
    return parsed


def _validate_publish_plan_action(
    loop_root: Path,
    manifest: dict[str, object],
    staging: Path,
    action: object,
    inventory_paths: set[str],
) -> None:
    if not isinstance(action, dict):
        raise _corrupt_manifest("publish plan action fields are invalid")
    if action.get("mode") == "quarantine_only":
        if set(action) != {"source", "destination", "mode"}:
            raise _corrupt_manifest("quarantine-only plan action fields are invalid")
        source = action["source"]
        if not isinstance(source, str) or source not in inventory_paths:
            raise _corrupt_manifest("publish plan source is not inventoried")
        _validate_relative_path(source, "publish plan source")
        if action["destination"] != "quarantine_only":
            raise _corrupt_manifest("quarantine-only plan destination is invalid")
        if manifest["source_kind"] not in ("global", "project"):
            raise _corrupt_manifest("quarantine-only plan source kind is invalid")
        if (
            manifest["source_kind"] == "project"
            and source in _PROJECT_AUTHORITY_SOURCES
        ):
            raise _corrupt_manifest("project authority cannot be quarantine-only")
        return
    common_fields = {
        "destination",
        "mode",
        "target_relative_path",
        "baseline",
        "candidate",
    }
    aggregate_project_merge = (
        manifest["source_kind"] == "project"
        and action.get("destination") == "project/project.md"
        and action.get("mode") == "merge_entries"
    )
    if aggregate_project_merge:
        action_fields = common_fields | {
            "sources",
            "source_entry_sha256_by_section",
            "candidate_entry_sha256_by_section",
        }
    else:
        action_fields = common_fields | {
            "source",
            "source_entry_sha256",
            "candidate_entry_sha256",
        }
    if set(action) != action_fields:
        raise _corrupt_manifest("publish plan action fields are invalid")
    destination = action["destination"]
    mode = action["mode"]
    target_relative_path = action["target_relative_path"]
    if aggregate_project_merge:
        sources = action["sources"]
        if not isinstance(sources, list) or not sources:
            raise _corrupt_manifest("project merge plan sources are invalid")
        source_paths: list[str] = []
        for record in sources:
            if not isinstance(record, dict) or set(record) != {"source", "section"}:
                raise _corrupt_manifest("project merge plan source is invalid")
            source = record["source"]
            section = record["section"]
            if not isinstance(source, str) or source not in inventory_paths:
                raise _corrupt_manifest("publish plan source is not inventoried")
            _validate_relative_path(source, "publish plan source")
            if section not in PROJECT_SECTIONS:
                raise _corrupt_manifest("publish plan project section is invalid")
            source_paths.append(source)
        if len(source_paths) != len(set(source_paths)):
            raise _corrupt_manifest("project merge plan contains duplicate sources")
    else:
        source = action["source"]
        if not isinstance(source, str) or source not in inventory_paths:
            raise _corrupt_manifest("publish plan source is not inventoried")
        _validate_relative_path(source, "publish plan source")
    if not isinstance(destination, str):
        raise _corrupt_manifest("publish plan destination is invalid")
    if manifest["source_kind"] == "project":
        if destination == "project/project.md":
            pass
        elif not _is_project_archive_action(source, destination, mode):
            raise _corrupt_manifest("project archive destination is invalid")
    _validate_relative_path(destination, "publish plan destination")
    if mode not in ("copy", "merge_entries"):
        raise _corrupt_manifest("publish plan mode is invalid")
    _validate_relative_path(target_relative_path, "publish plan target")
    try:
        expected_target = _classification_destination(
            loop_root,
            manifest,
            destination,
        )
    except LoopMemoryError as error:
        raise _corrupt_manifest("publish plan destination is invalid") from error
    if is_reserved_product_path(expected_target):
        raise _corrupt_manifest("publish plan target is product-owned")
    if target_relative_path != expected_target.relative_to(loop_root).as_posix():
        raise _corrupt_manifest("publish plan target does not match destination")

    baseline = action["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {
        "exists",
        "sha256",
        "size",
    }:
        raise _corrupt_manifest("publish plan baseline is invalid")
    if not isinstance(baseline["exists"], bool):
        raise _corrupt_manifest("publish plan baseline existence is invalid")
    if baseline["exists"]:
        _validate_hash_size(
            baseline["sha256"],
            baseline["size"],
            "publish plan baseline",
        )
    elif baseline["sha256"] is not None or baseline["size"] is not None:
        raise _corrupt_manifest("missing publish baseline has content metadata")

    candidate = action["candidate"]
    if not isinstance(candidate, dict) or set(candidate) != {
        "relative_path",
        "sha256",
        "size",
    }:
        raise _corrupt_manifest("publish plan candidate is invalid")
    candidate_relative_path = candidate["relative_path"]
    _validate_relative_path(candidate_relative_path, "publish plan candidate")
    if candidate_relative_path != f"candidates/{target_relative_path}":
        raise _corrupt_manifest("publish plan candidate path is invalid")
    _validate_hash_size(
        candidate["sha256"],
        candidate["size"],
        "publish plan candidate",
    )
    candidate_path = _safe_staging_path(staging, candidate_relative_path)
    try:
        _assert_regular_file(candidate_path, "migration staged candidate")
        candidate_content = candidate_path.read_bytes()
    except FileNotFoundError as error:
        raise _corrupt_manifest("migration staged candidate is missing") from error
    if (
        len(candidate_content) != candidate["size"]
        or hashlib.sha256(candidate_content).hexdigest() != candidate["sha256"]
    ):
        raise _corrupt_manifest("migration staged candidate changed")

    if aggregate_project_merge:
        source_mapping = action["source_entry_sha256_by_section"]
        candidate_mapping = action["candidate_entry_sha256_by_section"]
        _validate_entry_hash_mapping(source_mapping, "project merge source entries")
        _validate_entry_hash_mapping(
            candidate_mapping,
            "project merge candidate entries",
        )
        try:
            candidate_text = candidate_content.decode("utf-8")
            actual_candidate_mapping = _project_entry_hash_mapping(
                candidate_text,
                candidate_path,
            )
        except (UnicodeDecodeError, LoopMemoryError) as error:
            raise _corrupt_manifest("project merge candidate is not canonical") from error
        if candidate_mapping != actual_candidate_mapping:
            raise _corrupt_manifest("project merge candidate hashes do not match")
        source_root = _manifest_snapshot_root(loop_root, manifest)
        actual_source_sets = {section: set() for section in PROJECT_SECTIONS}
        for record in sources:
            source_path = source_root.joinpath(
                *PurePosixPath(record["source"]).parts
            )
            try:
                _assert_regular_file(source_path, "migration source")
                source_text = source_path.read_text(encoding="utf-8")
                source_hashes = _entry_hashes(
                    _entry_blocks(source_text, source_path)
                )
            except (FileNotFoundError, UnicodeDecodeError, LoopMemoryError) as error:
                raise _corrupt_manifest("project merge source is invalid") from error
            actual_source_sets[record["section"]].update(source_hashes)
        actual_source_mapping = {
            section: sorted(actual_source_sets[section])
            for section in PROJECT_SECTIONS
        }
        if source_mapping != actual_source_mapping:
            raise _corrupt_manifest("project merge source hashes do not match")
        for section in PROJECT_SECTIONS:
            if not set(source_mapping[section]).issubset(candidate_mapping[section]):
                raise _corrupt_manifest("project merge candidate misses source entries")
        return

    source_hashes = action["source_entry_sha256"]
    candidate_hashes = action["candidate_entry_sha256"]
    _validate_entry_hashes(source_hashes, "publish plan source entries")
    _validate_entry_hashes(candidate_hashes, "publish plan candidate entries")
    if mode == "copy" and (source_hashes or candidate_hashes):
        raise _corrupt_manifest("copy publish action has entry hashes")
    if mode == "copy" and destination == "project/project.md":
        try:
            _validate_project_document(candidate_content, candidate_path)
        except LoopMemoryError as error:
            raise _corrupt_manifest("project copy candidate is not canonical") from error
    if mode == "merge_entries":
        try:
            candidate_blocks = _action_entry_blocks(
                action,
                candidate_content,
                candidate_path,
            )
        except (UnicodeDecodeError, LoopMemoryError) as error:
            raise _corrupt_manifest("merge candidate entries are invalid") from error
        if candidate_hashes != _entry_hashes(candidate_blocks):
            raise _corrupt_manifest("merge candidate entry hashes do not match")
        if not set(source_hashes).issubset(candidate_hashes):
            raise _corrupt_manifest("merge candidate is missing a source entry")
        source_root = _manifest_snapshot_root(loop_root, manifest)
        source_path = source_root.joinpath(*PurePosixPath(source).parts)
        try:
            _assert_regular_file(source_path, "migration source")
            source_text = source_path.read_text(encoding="utf-8")
            actual_source_hashes = _entry_hashes(
                _entry_blocks(source_text, source_path)
            )
        except (FileNotFoundError, UnicodeDecodeError, LoopMemoryError) as error:
            raise _corrupt_manifest("merge source entries are invalid") from error
        if source_hashes != actual_source_hashes:
            raise _corrupt_manifest("merge source entry hashes do not match")


def _validate_entry_hash_mapping(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(PROJECT_SECTIONS):
        raise _corrupt_manifest(f"{label} mapping is invalid")
    for section in PROJECT_SECTIONS:
        _validate_entry_hashes(value[section], f"{label} {section}")


def _validate_entry_hashes(value: object, label: str) -> None:
    if not isinstance(value, list) or not all(
        isinstance(digest, str) and _HASH.fullmatch(digest)
        for digest in value
    ):
        raise _corrupt_manifest(f"{label} hashes are invalid")
    if value != sorted(set(value)):
        raise _corrupt_manifest(f"{label} hashes must be sorted and unique")


def _validate_hash_size(digest: object, size: object, label: str) -> None:
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise _corrupt_manifest(f"{label} hash is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise _corrupt_manifest(f"{label} size is invalid")


def _validate_staging_tree(
    staging: Path,
    expected_files: set[str],
) -> None:
    expected_directories: set[str] = set()
    for relative_path in expected_files:
        parent = PurePosixPath(relative_path).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    pending = [staging]
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            entry_path = Path(entry.path)
            entry_stat = entry.stat(follow_symlinks=False)
            relative_path = entry_path.relative_to(staging).as_posix()
            if stat.S_ISLNK(entry_stat.st_mode):
                raise LoopMemoryError(
                    code="unsafe_path",
                    message=f"Migration staging contains a symlink: {entry_path}",
                    recoverable=False,
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                actual_directories.add(relative_path)
                pending.append(entry_path)
            elif stat.S_ISREG(entry_stat.st_mode):
                actual_files.add(relative_path)
            else:
                raise _corrupt_manifest("migration staging contains a special file")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise _corrupt_manifest("migration staging tree does not match publish plan")


def _project_memory_template() -> str:
    return (
        "# Project Memory\n\n"
        + "\n\n".join(f"## {section}" for section in PROJECT_SECTIONS)
        + "\n"
    )


def _project_section(
    text: str,
    section: str,
    path: Path,
) -> tuple[int, int, list[str]]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "# Project Memory":
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Project memory title is not canonical: {path}",
            recoverable=False,
        )
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").startswith("## ")
    ]
    headings = [
        lines[index].rstrip("\r\n")[3:]
        for index in heading_indexes
    ]
    if headings != list(PROJECT_SECTIONS):
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Project memory sections are not canonical: {path}",
            recoverable=False,
        )
    try:
        section_position = headings.index(section)
    except ValueError as error:
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Project memory section is missing: {section!r}",
            recoverable=False,
        ) from error
    heading_index = heading_indexes[section_position]
    end_line = (
        heading_indexes[section_position + 1]
        if section_position + 1 < len(heading_indexes)
        else len(lines)
    )
    section_start = offsets[heading_index] + len(lines[heading_index])
    section_end = offsets[end_line] if end_line < len(lines) else len(text)
    return (
        section_start,
        section_end,
        _top_level_bullet_blocks(lines, heading_index + 1, end_line),
    )


def _validate_project_document(content: bytes, path: Path) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Project memory is not UTF-8: {path}",
            recoverable=False,
        ) from error
    _project_section(text, PROJECT_SECTIONS[0], path)


def _project_entry_hash_mapping(
    text: str,
    path: Path,
) -> dict[str, list[str]]:
    return {
        section: _entry_hashes(_project_section(text, section, path)[2])
        for section in PROJECT_SECTIONS
    }


def _merge_project_candidate(
    source: Path,
    baseline_content: bytes | None,
    section: str,
    destination: Path,
) -> tuple[bytes, list[str], list[str]]:
    try:
        source_text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Entry source is not UTF-8: {source}",
            recoverable=False,
        ) from error
    source_blocks = _entry_blocks(source_text, source)
    if baseline_content is None:
        destination_text = _project_memory_template()
    else:
        try:
            destination_text = baseline_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LoopMemoryError(
                code="migration_conflict",
                message=f"Project target is not UTF-8: {destination}",
                recoverable=False,
            ) from error
    _, section_end, destination_blocks = _project_section(
        destination_text,
        section,
        destination,
    )
    candidate_text = _merge_blocks_into_section(
        destination_text,
        section_end,
        destination_blocks,
        source_blocks,
    )
    _, _, candidate_blocks = _project_section(
        candidate_text,
        section,
        destination,
    )
    return (
        candidate_text.encode("utf-8"),
        _entry_hashes(source_blocks),
        _entry_hashes(candidate_blocks),
    )


def _merge_entries_candidate(
    source: Path,
    source_content: bytes,
    baseline_content: bytes | None,
    classified_destination: str,
    destination: Path,
) -> tuple[bytes, list[str], list[str]]:
    try:
        source_text = source_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Entry source is not UTF-8: {source}",
            recoverable=False,
        ) from error
    source_blocks = _entry_blocks(source_text, source)
    if baseline_content is not None:
        try:
            destination_text = baseline_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LoopMemoryError(
                code="migration_conflict",
                message=f"Entry target is not UTF-8: {destination}",
                recoverable=False,
            ) from error
    else:
        destination_text = _entries_template(classified_destination)
    _, section_end, destination_blocks = _entries_section(
        destination_text,
        destination,
    )
    candidate_text = _merge_blocks_into_section(
        destination_text,
        section_end,
        destination_blocks,
        source_blocks,
    )
    candidate_blocks = _entry_blocks(candidate_text, destination)
    return (
        candidate_text.encode("utf-8"),
        _entry_hashes(source_blocks),
        _entry_hashes(candidate_blocks),
    )


def _merge_blocks_into_section(
    destination_text: str,
    section_end: int,
    destination_blocks: list[str],
    source_blocks: list[str],
) -> str:
    existing = set(destination_blocks)
    missing: list[str] = []
    for block in source_blocks:
        if block in existing:
            continue
        existing.add(block)
        missing.append(block)
    if not missing:
        return destination_text
    prefix = destination_text[:section_end]
    suffix = destination_text[section_end:]
    separator = ""
    if prefix and not prefix.endswith("\n"):
        separator += "\n"
    if prefix and not prefix.endswith("\n\n"):
        separator += "\n"
    insertion = "\n\n".join(missing) + "\n"
    return prefix + separator + insertion + suffix


def _entry_hashes(blocks: list[str]) -> list[str]:
    return sorted(
        {
            hashlib.sha256(block.encode("utf-8")).hexdigest()
            for block in blocks
        }
    )


def _publish_staged(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    plan = _load_publish_plan(loop_root, manifest)
    publish_actions = [
        action
        for action in plan["actions"]
        if action["mode"] != "quarantine_only"
    ]
    states: list[str] = []
    for action in publish_actions:
        states.append(_publish_state(loop_root, action))

    staging = Path(manifest["staging_path"])
    for action, state in zip(publish_actions, states, strict=True):
        if state == "baseline":
            _publish_candidate(loop_root, manifest, staging, action)
    _verify_published_plan(loop_root, plan)


def _publish_candidate(
    loop_root: Path,
    manifest: dict[str, object],
    staging: Path,
    action: dict[str, object],
) -> None:
    state = _publish_state(loop_root, action)
    if state == "candidate":
        return
    target = _plan_target(loop_root, action)
    candidate_path = _safe_staging_path(
        staging,
        action["candidate"]["relative_path"],
    )
    content = candidate_path.read_bytes()
    candidate = action["candidate"]
    if (
        len(content) != candidate["size"]
        or hashlib.sha256(content).hexdigest() != candidate["sha256"]
    ):
        raise _corrupt_manifest("migration staged candidate changed before publish")
    # Cooperative global/project writers share promotion leases. Mutations after
    # this final check by protocol-external writers are outside that boundary.
    ensure_directory(target.parent)
    if action["baseline"]["exists"]:
        _write_bytes_atomic_replace(target, content)
    else:
        _write_bytes_no_replace(target, content)


def _ensure_canonical_target_layout(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    source_kind = manifest["source_kind"]
    if source_kind == "global":
        ensure_global_layout(loop_root)
    elif source_kind in ("project", "session"):
        ensure_project_layout(loop_root, manifest["project_id"])


def _plan_target(
    loop_root: Path,
    action: dict[str, object],
) -> Path:
    relative_path = action["target_relative_path"]
    _validate_relative_path(relative_path, "publish plan target")
    target = _safe_loop_path(
        loop_root,
        loop_root.joinpath(*PurePosixPath(relative_path).parts),
    )
    if is_reserved_product_path(target):
        raise _corrupt_manifest("publish plan target is product-owned")
    return target


def _publish_state(
    loop_root: Path,
    action: dict[str, object],
) -> str:
    target = _plan_target(loop_root, action)
    baseline = action["baseline"]
    candidate = action["candidate"]
    if not target.exists() and not target.is_symlink():
        if baseline["exists"]:
            raise _target_changed(target)
        return "baseline"
    try:
        _assert_regular_file(target, "migration target")
        content = target.read_bytes()
    except (FileNotFoundError, LoopMemoryError) as error:
        raise _target_changed(target) from error
    digest = hashlib.sha256(content).hexdigest()
    if len(content) == candidate["size"] and digest == candidate["sha256"]:
        return "candidate"
    if action["mode"] == "merge_entries" and _is_merge_candidate_superset(
        action,
        content,
        target,
    ):
        return "candidate"
    if (
        baseline["exists"]
        and len(content) == baseline["size"]
        and digest == baseline["sha256"]
    ):
        return "baseline"
    raise _target_changed(target)


def _verify_published_plan(
    loop_root: Path,
    plan: dict[str, object],
) -> None:
    for action in plan["actions"]:
        if action["mode"] == "quarantine_only":
            continue
        target = _plan_target(loop_root, action)
        candidate = action["candidate"]
        try:
            _assert_regular_file(target, "migration target")
            content = target.read_bytes()
        except (FileNotFoundError, LoopMemoryError) as error:
            raise _target_changed(target) from error
        if action["mode"] == "merge_entries":
            if not _is_merge_candidate_superset(action, content, target):
                raise _target_changed(target)
        elif (
            len(content) != candidate["size"]
            or hashlib.sha256(content).hexdigest() != candidate["sha256"]
        ):
            raise _target_changed(target)


def _is_merge_candidate_superset(
    action: dict[str, object],
    content: bytes,
    path: Path,
) -> bool:
    if "candidate_entry_sha256_by_section" in action:
        try:
            text = content.decode("utf-8")
            current_mapping = _project_entry_hash_mapping(text, path)
        except (UnicodeDecodeError, LoopMemoryError):
            return False
        candidate_mapping = action["candidate_entry_sha256_by_section"]
        return all(
            set(candidate_mapping[section]).issubset(current_mapping[section])
            for section in PROJECT_SECTIONS
        )
    try:
        current_hashes = set(
            _entry_hashes(_action_entry_blocks(action, content, path))
        )
    except (UnicodeDecodeError, LoopMemoryError):
        return False
    return set(action["candidate_entry_sha256"]).issubset(current_hashes)


def _action_entry_blocks(
    action: dict[str, object],
    content: bytes,
    path: Path,
) -> list[str]:
    text = content.decode("utf-8")
    section = action.get("section")
    if section is not None:
        _, _, blocks = _project_section(text, section, path)
        return blocks
    return _entry_blocks(text, path)


def _target_changed(path: Path) -> LoopMemoryError:
    return LoopMemoryError(
        code="target_changed",
        message=f"Migration target changed from its publish plan: {path}",
        recoverable=False,
    )


def _entry_blocks(text: str, path: Path) -> list[str]:
    _, _, blocks = _entries_section(text, path)
    return blocks


def _entries_section(text: str, path: Path) -> tuple[int, int, list[str]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    headings = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == "## Entries"
    ]
    if not headings and path.name == "long.md" and path.parent.name == "global":
        headings = [
            index
            for index, line in enumerate(lines)
            if line.rstrip("\r\n") == "## Methodology"
        ]
    if len(headings) != 1:
        raise LoopMemoryError(
            code="migration_conflict",
            message=(
                "Memory file must contain exactly one ## Entries or ## Methodology "
                f"section: {path}"
            ),
            recoverable=False,
        )
    heading = headings[0]
    section_start = offsets[heading] + len(lines[heading])
    section_end = len(text)
    end_line = len(lines)
    for index in range(heading + 1, len(lines)):
        if lines[index].startswith("## "):
            section_end = offsets[index]
            end_line = index
            break
    blocks = _top_level_bullet_blocks(lines, heading + 1, end_line)
    return section_start, section_end, blocks


def _top_level_bullet_blocks(
    lines: list[str],
    start_line: int,
    end_line: int,
) -> list[str]:
    bullet_lines = [
        index
        for index in range(start_line, end_line)
        if lines[index].startswith("- ")
    ]
    blocks: list[str] = []
    for position, start in enumerate(bullet_lines):
        end = bullet_lines[position + 1] if position + 1 < len(bullet_lines) else end_line
        block = "".join(lines[start:end]).strip("\r\n")
        if block:
            blocks.append(block)
    return blocks


def _entries_template(destination: str) -> str:
    if destination == "global/long.md":
        return global_facts.LONG_TEMPLATE
    if destination == "global/medium.md":
        return "# Global Medium-Term Memory\n\n## Entries\n"
    if destination == "global/short.md":
        return "# Global Short-Term Memory\n\n## Entries\n"
    return "# Project Memory\n\n## Entries\n"


def _write_bytes_no_replace(path: Path, content: bytes) -> None:
    ensure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    identity: tuple[int, int] | None = None
    try:
        stream = temporary.open("xb")
        with stream:
            try:
                identity = _path_identity_from_descriptor(stream.fileno())
            except BaseException:
                identity = _path_identity_from_open_descriptor(stream.fileno())
                raise
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
                return
            raise LoopMemoryError(
                code="migration_conflict",
                message=f"Migration target appeared with different content: {path}",
                recoverable=False,
            )
        _fsync_directory(path.parent)
    finally:
        if identity is not None:
            _unlink_path_if_identity(temporary, identity)


def _path_identity_from_descriptor(descriptor: int) -> tuple[int, int]:
    value = os.fstat(descriptor)
    return value.st_dev, value.st_ino


def _path_identity_from_open_descriptor(
    descriptor: int,
) -> tuple[int, int] | None:
    try:
        value = os.stat(descriptor)
    except BaseException:
        return None
    return value.st_dev, value.st_ino


def _unlink_path_if_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    if (value.st_dev, value.st_ino) != expected:
        return False
    path.unlink()
    return True


def _target_file_record(loop_root: Path, path: Path) -> dict[str, object]:
    _assert_regular_file(path, "migration target")
    content = path.read_bytes()
    return {
        "relative_path": path.relative_to(loop_root).as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _verify_pre_quarantine_state(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    if manifest.get("schema_version") == 2:
        _verify_inventory(
            _manifest_snapshot_root(loop_root, manifest),
            manifest["files"],
            "corrupt_state",
        )
        return
    source = Path(manifest["source"])
    quarantine = _quarantine_source(loop_root, manifest)
    source_exists = _real_directory_exists(source)
    quarantine_exists = _real_directory_exists(quarantine)
    if not source_exists or quarantine_exists:
        raise _corrupt_manifest("pre-quarantine source invariant failed")
    _verify_inventory(source, manifest["files"], "source_changed")
    _verify_quarantine_device(loop_root, manifest, source)


def _verify_detected_state(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    source = Path(manifest["source"])
    quarantine = _quarantine_source(loop_root, manifest)
    if not _real_directory_exists(source) or _real_directory_exists(quarantine):
        raise _corrupt_manifest("detected source invariant failed")
    if _contains_symlink(source):
        raise LoopMemoryError(
            code="unsafe_legacy_source",
            message=f"Detected source contains a symlink: {source}",
            recoverable=False,
        )


def _verify_references_updated_state(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    if manifest.get("schema_version") == 2:
        _verify_inventory(
            _manifest_snapshot_root(loop_root, manifest),
            manifest["files"],
            "corrupt_state",
        )
        _verify_target_files(loop_root, manifest)
        return
    source = Path(manifest["source"])
    quarantine = _quarantine_source(loop_root, manifest)
    source_exists = _real_directory_exists(source)
    quarantine_exists = _real_directory_exists(quarantine)
    if source_exists == quarantine_exists:
        raise _corrupt_manifest(
            "references_updated must have exactly one source or quarantine copy"
        )
    if source_exists:
        _verify_inventory(source, manifest["files"], "source_changed")
        _verify_quarantine_device(loop_root, manifest, source)
    else:
        _verify_inventory(quarantine, manifest["files"], "corrupt_state")
    _verify_target_files(loop_root, manifest)


def _verify_quarantined_state(
    loop_root: Path,
    manifest: dict[str, object],
    *,
    require_alias: bool,
) -> None:
    source = Path(manifest["source"])
    if manifest.get("schema_version") == 2:
        snapshot = _manifest_snapshot_root(loop_root, manifest)
        retained = manifest.get("quarantine_path")
        if isinstance(retained, str) and not Path(retained).is_absolute():
            retained = str(_safe_loop_path(loop_root, loop_root / retained))
        if retained != str(snapshot):
            raise _corrupt_manifest("retained evidence path does not match snapshot")
        _verify_inventory(snapshot, manifest["files"], "corrupt_state")
        _verify_target_files(loop_root, manifest)
        if require_alias:
            alias = RegistryStore(loop_root).resolve_legacy_alias(source)
            expected = {
                "target": manifest["target"],
                "migration_id": manifest["migration_id"],
            }
            if alias != expected:
                raise _corrupt_manifest("legacy alias is missing or inconsistent")
        return
    quarantine = _quarantine_source(loop_root, manifest)
    if not _real_directory_exists(quarantine):
        raise _corrupt_manifest("quarantined source invariant failed")
    if manifest.get("quarantine_path") != str(quarantine):
        raise _corrupt_manifest("quarantine_path does not match migration location")
    # A schema-v1 quarantine is already internal custody.  `source` remains
    # provenance only; never probe the external path while recovering it.
    _verify_inventory(quarantine, manifest["files"], "corrupt_state")
    _verify_target_files(loop_root, manifest)
    if require_alias:
        alias = RegistryStore(loop_root).resolve_legacy_alias(source)
        expected = {
            "target": manifest["target"],
            "migration_id": manifest["migration_id"],
        }
        if alias != expected:
            raise _corrupt_manifest("legacy alias is missing or inconsistent")


def _verify_inventory(
    root: Path,
    expected: object,
    error_code: str,
) -> None:
    if not _real_directory_exists(root) or _contains_symlink(root):
        raise LoopMemoryError(
            code="unsafe_legacy_source" if error_code == "source_changed" else error_code,
            message=f"Migration inventory root is missing or unsafe: {root}",
            recoverable=False,
        )
    actual, _ = _inventory_files(root)
    if actual != expected:
        raise LoopMemoryError(
            code=error_code,
            message=f"Migration inventory changed: {root}",
            recoverable=False,
        )


def _verify_target_files(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    plan = _load_publish_plan(loop_root, manifest)
    if manifest["state"] == "copied":
        for action in plan["actions"]:
            if action["mode"] == "quarantine_only":
                continue
            _publish_state(loop_root, action)
        return

    for action in plan["actions"]:
        if action["mode"] == "quarantine_only":
            continue
        path = _plan_target(loop_root, action)
        try:
            _assert_regular_file(path, "migration target")
            content = path.read_bytes()
        except (FileNotFoundError, LoopMemoryError) as error:
            raise _target_changed(path) from error
        candidate = action["candidate"]
        if action["mode"] == "copy":
            if (
                len(content) != candidate["size"]
                or hashlib.sha256(content).hexdigest() != candidate["sha256"]
            ):
                raise _target_changed(path)
            continue
        if "candidate_entry_sha256_by_section" in action:
            try:
                text = content.decode("utf-8")
                current_mapping = _project_entry_hash_mapping(text, path)
            except (UnicodeDecodeError, LoopMemoryError) as error:
                raise _target_changed(path) from error
            source_mapping = action["source_entry_sha256_by_section"]
            candidate_mapping = action["candidate_entry_sha256_by_section"]
            for section in PROJECT_SECTIONS:
                required_hashes = set(source_mapping[section]) | set(
                    candidate_mapping[section]
                )
                if not required_hashes.issubset(current_mapping[section]):
                    raise _target_changed(path)
            continue
        try:
            blocks = _action_entry_blocks(action, content, path)
        except (UnicodeDecodeError, LoopMemoryError) as error:
            raise _target_changed(path) from error
        actual_hashes = {
            hashlib.sha256(block.encode("utf-8")).hexdigest()
            for block in blocks
        }
        required_hashes = set(action["source_entry_sha256"]) | set(
            action["candidate_entry_sha256"]
        )
        missing_hashes = required_hashes - actual_hashes
        if missing_hashes:
            archived = (
                path == loop_root / "global" / "long.md"
                and global_facts.verify_receipt_coverage(
                    loop_root,
                    missing_hashes,
                )
            )
            if not archived:
                raise _target_changed(path)


def _quarantine_source(
    loop_root: Path,
    manifest: dict[str, object],
) -> Path:
    return _safe_loop_path(
        loop_root,
        loop_root
        / "migrations"
        / "quarantine"
        / manifest["migration_id"]
        / "source",
    )


def _manifest_snapshot_root(
    loop_root: Path,
    manifest: dict[str, object],
) -> Path:
    if manifest.get("schema_version") == 2:
        return _manifest_custody_snapshot(loop_root, manifest).path
    # A version-one source already held beneath the old custody directory is
    # internal evidence. It remains where it is and is never restored outside.
    quarantine = _quarantine_source(loop_root, manifest)
    if _real_directory_exists(quarantine):
        return quarantine
    raise _legacy_stage_required()


@dataclass(frozen=True)
class ManifestCustodySnapshot:
    path: Path
    files: list[dict[str, object]]
    inventory_sha256: str
    has_credential_assignment: bool
    snapshot_identity: tuple[int, int, int, int, int, int, int]
    receipt_identity: tuple[int, int, int, int, int, int, int]
    receipt_sha256: str
    node_identities: tuple[
        tuple[str, tuple[int, int, int, int, int, int, int]], ...
    ]


def _manifest_custody_snapshot(
    loop_root: Path,
    manifest: dict[str, object],
) -> ManifestCustodySnapshot:
    """Validate one schema-v2 manifest using only Loop-owned custody."""
    if manifest.get("schema_version") != 2:
        raise _legacy_stage_required()
    root = Path(loop_root).resolve(strict=False)
    snapshot_value = manifest.get("snapshot")
    if not isinstance(snapshot_value, str) or not snapshot_value:
        raise _corrupt_manifest("schema 2 snapshot is missing")
    snapshot_path = Path(snapshot_value)
    try:
        snapshot = _safe_loop_path(
            root,
            snapshot_path if snapshot_path.is_absolute() else root / snapshot_path,
        )
    except LoopMemoryError as error:
        raise _corrupt_manifest("custody snapshot identity is unsafe") from error
    try:
        relative_snapshot = snapshot.relative_to(root).as_posix()
    except ValueError as error:
        raise _corrupt_manifest("custody snapshot is outside loop root") from error
    legacy_match = _LEGACY_SNAPSHOT.fullmatch(relative_snapshot)
    quarantine_match = _V1_QUARANTINE_SNAPSHOT.fullmatch(relative_snapshot)
    if not legacy_match and not quarantine_match:
        raise _corrupt_manifest("custody snapshot path is not canonical")
    if quarantine_match and quarantine_match.group(1) != manifest.get("migration_id"):
        raise _corrupt_manifest("custody snapshot migration binding is invalid")
    try:
        snapshot_parent_stat = snapshot.parent.lstat()
    except FileNotFoundError as error:
        raise _legacy_stage_required() from error
    if stat.S_ISLNK(snapshot_parent_stat.st_mode) or not stat.S_ISDIR(snapshot_parent_stat.st_mode):
        raise _corrupt_manifest("custody snapshot parent is unsafe")
    if snapshot_parent_stat.st_uid != os.getuid():
        raise _corrupt_manifest("custody snapshot parent is not owned by the current user")
    try:
        first_node_identities = _custody_node_identities(snapshot)
    except LoopMemoryError as error:
        raise _corrupt_manifest("custody snapshot identity is unsafe") from error
    snapshot_identity = dict(first_node_identities)[""]
    receipt_path = snapshot.parent / "receipt.json"
    receipt = None
    receipt_identity = (0, 0, 0, 0, 0, 0, 0)
    receipt_bytes = b""
    if legacy_match or receipt_path.exists():
        receipt, receipt_identity, receipt_bytes = _read_custody_receipt(
            receipt_path,
            manifest,
        )
    files_digest = _inventory_sha256(manifest["files"])
    if manifest.get("source_inventory_sha256") != files_digest:
        raise _corrupt_manifest("source inventory digest does not match files")
    try:
        snapshot_files, has_credentials = _inventory_files(snapshot)
    except LoopMemoryError as error:
        raise _corrupt_manifest("custody snapshot is unsafe") from error
    snapshot_digest = _inventory_sha256(snapshot_files)
    if snapshot_files != manifest["files"] or snapshot_digest != files_digest:
        raise _corrupt_manifest("custody snapshot inventory does not match manifest")
    try:
        second_node_identities = _custody_node_identities(snapshot)
    except LoopMemoryError as error:
        raise _corrupt_manifest("custody snapshot identity is unsafe") from error
    if second_node_identities != first_node_identities:
        raise _corrupt_manifest("custody snapshot changed during validation")
    credential_fixed = "credential_assignment" in manifest.get(
        "protection_reasons", []
    )
    if has_credentials and not credential_fixed:
        raise _corrupt_manifest("custody risk does not match fixed manifest metadata")
    expected_importable = not credential_fixed
    if receipt is not None:
        if receipt["importable"] is not expected_importable:
            raise _corrupt_manifest("custody receipt importability does not match inventory")
        expected_reasons = ["credential_assignment"] if credential_fixed else []
        if receipt["protection_reasons"] != expected_reasons:
            raise _corrupt_manifest("custody receipt protection metadata is inconsistent")
        if receipt["source_path"] != manifest["source"]:
            raise _corrupt_manifest("custody receipt source provenance is inconsistent")
        if receipt["inventory_sha256"] != files_digest:
            raise _corrupt_manifest("custody receipt inventory digest is inconsistent")
    return ManifestCustodySnapshot(
        path=snapshot,
        files=[dict(record) for record in snapshot_files],
        inventory_sha256=snapshot_digest,
        has_credential_assignment=credential_fixed,
        snapshot_identity=snapshot_identity,
        receipt_identity=receipt_identity,
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        node_identities=first_node_identities,
    )


def _custody_node_identities(
    root: Path,
) -> tuple[tuple[str, tuple[int, int, int, int, int, int, int]], ...]:
    root_stat = _source_root_stat(root, missing_ok=False)
    if root_stat is None or not stat.S_ISDIR(root_stat.st_mode):
        raise _source_unstable()
    _verify_current_user_owner(root_stat)
    identities = {"": _full_identity(root_stat)}
    directory_flags, _ = _descriptor_flags()
    with _open_descriptor(root, directory_flags) as root_descriptor:
        opened = _checked_fstat(root_descriptor)
        _verify_current_user_owner(opened)
        if _full_identity(opened) != identities[""]:
            raise _source_unstable()

        def collect(directory_descriptor: int, parent_parts: tuple[str, ...]) -> None:
            try:
                with os.scandir(directory_descriptor) as entries:
                    for entry in entries:
                        name = entry.name
                        relative = PurePosixPath(*parent_parts, name).as_posix()
                        _validate_relative_path(relative, "custody snapshot")
                        value = os.stat(
                            name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                        if stat.S_ISLNK(value.st_mode) or not (
                            stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)
                        ):
                            raise _unsafe_legacy_source(
                                "Custody snapshot contains a symlink or special file"
                            )
                        _verify_current_user_owner(value)
                        identities[relative] = _full_identity(value)
                        if stat.S_ISDIR(value.st_mode):
                            with _open_descriptor(
                                name,
                                directory_flags,
                                dir_fd=directory_descriptor,
                            ) as child_descriptor:
                                child_opened = _checked_fstat(child_descriptor)
                                _verify_current_user_owner(child_opened)
                                if _full_identity(child_opened) != _full_identity(value):
                                    raise _source_unstable()
                                collect(child_descriptor, (*parent_parts, name))
                                live = os.stat(
                                    name,
                                    dir_fd=directory_descriptor,
                                    follow_symlinks=False,
                                )
                                if _full_identity(live) != _full_identity(value):
                                    raise _source_unstable()
            except LoopMemoryError:
                raise
            except OSError as error:
                raise _source_unstable() from error

        collect(root_descriptor, ())
        live_root = _source_root_stat(root, missing_ok=False)
        if live_root is None or _full_identity(live_root) != _full_identity(opened):
            raise _source_unstable()
    return tuple(sorted(identities.items()))


def _read_custody_receipt(
    path: Path,
    manifest: dict[str, object],
) -> tuple[dict[str, object], tuple[int, int, int, int, int, int, int], bytes]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise _corrupt_manifest("custody receipt is not a regular file")
        if before.st_uid != os.getuid():
            raise _corrupt_manifest("custody receipt is not owned by the current user")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            _verify_current_user_owner(opened)
            identity = _full_identity(opened)
            if identity != _full_identity(before):
                raise _corrupt_manifest("custody receipt changed during validation")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            live = path.lstat()
            if _full_identity(after) != identity or _full_identity(live) != identity:
                raise _corrupt_manifest("custody receipt changed during validation")
        finally:
            os.close(descriptor)
        receipt = json.loads(content)
    except LoopMemoryError:
        raise
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise _corrupt_manifest("custody receipt is unreadable") from error
    if not isinstance(receipt, dict):
        raise _corrupt_manifest("custody receipt is invalid")
    required = {"schema_version", "snapshot_id", "source_path", "inventory_sha256", "importable", "protection_reasons"}
    if set(receipt) != required:
        raise _corrupt_manifest("custody receipt fields are invalid")
    snapshot_id = Path(manifest["snapshot"]).parent.name
    if (
        receipt["schema_version"] not in (1, 2)
        or receipt["snapshot_id"] != snapshot_id
        or not isinstance(receipt["source_path"], str)
        or not Path(receipt["source_path"]).is_absolute()
        or not isinstance(receipt["inventory_sha256"], str)
        or not _HASH.fullmatch(receipt["inventory_sha256"])
        or not isinstance(receipt["importable"], bool)
        or not isinstance(receipt["protection_reasons"], list)
        or any(not isinstance(item, str) for item in receipt["protection_reasons"])
    ):
        raise _corrupt_manifest("custody receipt fields are invalid")
    return receipt, identity, content


def _verify_quarantine_device(
    loop_root: Path,
    manifest: dict[str, object],
    source: Path,
) -> None:
    quarantine_parent = _quarantine_source(loop_root, manifest).parent
    if _path_device(source) != _path_device(quarantine_parent):
        raise _cross_device_unsupported(source, quarantine_parent)


def _path_device(path: Path) -> int:
    current = path
    while True:
        try:
            return current.stat().st_dev
        except FileNotFoundError:
            if current.parent == current:
                raise
            current = current.parent


def _real_directory_exists(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise LoopMemoryError(
            code="unsafe_legacy_source",
            message=f"Migration source path is a symlink: {path}",
            recoverable=False,
        )
    if not stat.S_ISDIR(mode):
        raise _corrupt_manifest(f"expected a directory: {path}")
    return True


def _assert_regular_file(path: Path, label: str) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"{label} is not a regular file: {path}",
            recoverable=False,
        )


def _safe_loop_path(loop_root: Path, candidate: Path) -> Path:
    lexical = Path(os.path.abspath(candidate))
    resolved = assert_loop_path(loop_root, lexical)
    if lexical != resolved:
        raise LoopMemoryError(
            code="unsafe_path",
            message=f"Migration path traverses a symlink: {lexical}",
            recoverable=False,
        )
    return resolved


def _validate_manifest_location(
    loop_root: Path,
    manifest_path: Path,
    manifest: dict[str, object],
) -> None:
    expected = _safe_loop_path(
        loop_root,
        loop_root / "migrations" / "manifests" / f"{manifest['migration_id']}.json",
    )
    if manifest_path != expected:
        raise _corrupt_manifest("manifest path does not match migration_id")
    source = Path(manifest["source"])
    if _is_relative_to(source, loop_root) or _is_relative_to(loop_root, source):
        raise _corrupt_manifest("manifest source overlaps the loop root")
    target = Path(manifest["target"])
    _safe_loop_path(loop_root, target)
    kind = manifest["source_kind"]
    project_id = manifest["project_id"]
    if kind == "global":
        expected_target = loop_root / "global"
    elif kind in ("project", "empty"):
        expected_target = loop_root / "projects" / project_id
    else:
        expected_prefix = loop_root / "projects" / project_id / "sessions" / "archive"
        expected_target = target
        try:
            target.relative_to(expected_prefix)
        except ValueError as error:
            raise _corrupt_manifest("session target is outside its project archive") from error
    if target != expected_target:
        raise _corrupt_manifest("manifest target does not match the loop layout")

    for record in manifest.get("target_files") or []:
        relative_path = record["relative_path"]
        actual = _safe_loop_path(
            loop_root,
            loop_root.joinpath(*PurePosixPath(relative_path).parts),
        )
        if kind == "global":
            allowed = relative_path in {
                "global/long.md",
                "global/medium.md",
                "global/short.md",
            }
        elif kind == "project":
            project_archive = target / "sessions" / "archive"
            allowed = actual == target / "project.md" or (
                actual != project_archive
                and _is_relative_to(actual, project_archive)
            )
        elif kind == "session":
            allowed = actual != target and _is_relative_to(actual, target)
        else:
            allowed = False
        if not allowed:
            raise _corrupt_manifest("target_files contains a non-migration target")

    copied_or_later = _STATE_INDEX[manifest["state"]] >= _STATE_INDEX["copied"]
    if copied_or_later:
        expected_staging = _staging_path(loop_root, manifest)
        if manifest.get("staging_path") != str(expected_staging):
            raise _corrupt_manifest("staging_path does not match migration layout")


def _verify_classification_pin(
    manifest: dict[str, object],
    classification_sha256: str,
) -> None:
    stored = manifest.get("classification_sha256")
    if stored is not None and stored != classification_sha256:
        raise LoopMemoryError(
            code="classification_mismatch",
            message="Classification changed after migration copy began",
            recoverable=False,
        )


def _verify_plan_classification(
    loop_root: Path,
    manifest: dict[str, object],
    classification: dict[str, object],
) -> None:
    plan = _load_publish_plan(loop_root, manifest)
    classification_actions = [
        action
        for action in classification["actions"]
        if action["mode"] != "discard_empty"
    ]
    project_merge_actions = [
        action
        for action in classification_actions
        if manifest["source_kind"] == "project"
        and action["destination"] == "project/project.md"
        and action["mode"] == "merge_entries"
    ]
    if project_merge_actions:
        expected_sources = [
            {"source": action["source"], "section": action["section"]}
            for action in project_merge_actions
        ]
        if not plan["actions"]:
            raise _corrupt_manifest("project merge plan target is not aggregated")
        aggregate = plan["actions"][0]
        if (
            aggregate["mode"] != "merge_entries"
            or aggregate["destination"] != "project/project.md"
            or aggregate["sources"] != expected_sources
        ):
            raise _corrupt_manifest("project merge plan differs from classification")
        classification_actions = [
            action
            for action in classification_actions
            if action not in project_merge_actions
        ]
        plan_actions = plan["actions"][1:]
    else:
        plan_actions = plan["actions"]
    expected = [
        {
            "source": action["source"],
            "destination": action["destination"],
            "mode": action["mode"],
        }
        for action in classification_actions
    ]
    actual = [
        {
            "source": action["source"],
            "destination": action["destination"],
            "mode": action["mode"],
        }
        for action in plan_actions
    ]
    if actual != expected:
        raise _corrupt_manifest("publish plan differs from classification")


def _ledger_path(loop_root: Path) -> Path:
    return _safe_loop_path(loop_root, loop_root / "migrations" / "ledger.jsonl")


def read_ledger_events(loop_root: Path) -> list[dict[str, object]]:
    events, _ = _read_ledger_content(loop_root)
    return events


def validate_ledger_events(
    events: list[dict[str, object]],
    manifest: dict[str, object],
) -> None:
    _validate_ledger_event_schema(events)
    migration_id, manifest_state = _validate_ledger_manifest_reference(manifest)
    migration_events = [
        event
        for event in events
        if event["migration_id"] == migration_id
    ]
    states = [event["state"] for event in migration_events]
    current_index = _STATE_INDEX[manifest_state]
    if states and states[0] == "detected":
        start_index = _STATE_INDEX["detected"]
    else:
        start_index = (
            _STATE_INDEX["detected"]
            if manifest_state == "detected"
            else _STATE_INDEX["inventoried"]
        )
    expected = list(_STATES[start_index : current_index + 1])
    if states not in (expected, expected[:-1]):
        raise _corrupt_manifest(
            "ledger must be a continuous prefix ending at the manifest state"
        )


def _validate_ledger_event_schema(events: list[dict[str, object]]) -> None:
    if not isinstance(events, list):
        raise _corrupt_manifest("ledger events must be a list")
    seen: set[tuple[str, str]] = set()
    for event_number, event in enumerate(events, 1):
        if not isinstance(event, dict) or set(event) != _LEDGER_EVENT_FIELDS:
            raise _corrupt_manifest(f"ledger event is invalid at {event_number}")
        migration_id = event["migration_id"]
        state = event["state"]
        timestamp = event["timestamp"]
        if (
            not isinstance(migration_id, str)
            or not _MIGRATION_ID.fullmatch(migration_id)
            or not isinstance(state, str)
            or state not in _STATE_INDEX
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise _corrupt_manifest(
                f"ledger event fields are invalid at {event_number}"
            )
        key = (migration_id, state)
        if key in seen:
            raise _corrupt_manifest("ledger contains a duplicate migration state")
        seen.add(key)


def _validate_ledger_manifest_reference(
    manifest: dict[str, object],
) -> tuple[str, str]:
    if not isinstance(manifest, dict):
        raise _corrupt_manifest("ledger manifest reference must be an object")
    migration_id = manifest.get("migration_id")
    if not isinstance(migration_id, str) or not _MIGRATION_ID.fullmatch(migration_id):
        raise _corrupt_manifest("ledger manifest migration_id is invalid")
    state = manifest.get("state")
    if not isinstance(state, str) or state not in _STATE_INDEX:
        raise _corrupt_manifest("ledger manifest state is invalid")
    return migration_id, state


def _read_ledger(loop_root: Path) -> list[dict[str, object]]:
    return read_ledger_events(loop_root)


def _read_ledger_content(
    loop_root: Path,
) -> tuple[list[dict[str, object]], str]:
    path = _ledger_path(loop_root)
    try:
        raw_content = path.read_bytes()
    except FileNotFoundError:
        return [], ""
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _corrupt_manifest("ledger is not UTF-8") from error
    if content and not content.endswith("\n"):
        raise _corrupt_manifest("ledger does not end with a complete event")

    def reject_duplicate_keys(pairs):
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError(f"non-finite constant: {value}")

    events: list[dict[str, object]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line:
            raise _corrupt_manifest(f"ledger has an empty line at {line_number}")
        try:
            event = json.loads(
                line,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_constant,
            )
        except (ValueError, TypeError) as error:
            raise _corrupt_manifest(f"ledger JSON is invalid at {line_number}") from error
        events.append(event)
    _validate_ledger_event_schema(events)
    return events, content


def _validate_ledger(
    loop_root: Path,
    manifest: dict[str, object],
) -> None:
    events = _read_ledger(loop_root)
    validate_ledger_events(events, manifest)


def _ensure_ledger_event(
    loop_root: Path,
    migration_id: str,
    state: str,
) -> bool:
    path = _ledger_path(loop_root)
    events, old_content = _read_ledger_content(loop_root)
    if any(
        event["migration_id"] == migration_id and event["state"] == state
        for event in events
    ):
        return False
    ensure_directory(path.parent)
    event = {
        "migration_id": migration_id,
        "state": state,
        "timestamp": time.time(),
    }
    event_line = json.dumps(event, sort_keys=True, allow_nan=False) + "\n"
    existed = path.exists()
    try:
        write_text_atomic(path, old_content + event_line)
    except BaseException:
        _restore_ledger_after_failed_write(path, old_content, existed)
        raise
    return True


def _restore_ledger_after_failed_write(
    path: Path,
    old_content: str,
    existed: bool,
) -> None:
    old_bytes = old_content.encode("utf-8")
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if (existed and current == old_bytes) or (not existed and current is None):
        return
    try:
        if existed:
            write_text_atomic(path, old_content)
        else:
            path.unlink()
            _fsync_directory(path.parent)
    except BaseException:
        try:
            restored = path.read_bytes()
        except FileNotFoundError:
            restored = None
        if (existed and restored == old_bytes) or (not existed and restored is None):
            return
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        try:
            rename = library.renamex_np
        except AttributeError as error:
            raise _unsupported_atomic_rename() from error
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (source_bytes, destination_bytes, 0x4)
    elif sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as error:
            raise _unsupported_atomic_rename() from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = (-100, source_bytes, -100, destination_bytes, 0x1)
    else:
        raise _unsupported_atomic_rename()
    rename.restype = ctypes.c_int
    if rename(*arguments) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EXDEV:
        raise _cross_device_unsupported(source, destination)
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise LoopMemoryError(
            code="migration_conflict",
            message=f"Quarantine destination already exists: {destination}",
            recoverable=False,
        )
    if error_number in (
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        errno.EINVAL,
    ):
        raise _unsupported_atomic_rename()
    raise OSError(error_number, os.strerror(error_number), destination)


def _unsupported_atomic_rename() -> LoopMemoryError:
    return LoopMemoryError(
        code="migration_atomic_rename_unsupported",
        message="Atomic no-replace quarantine rename is unavailable",
        recoverable=False,
    )


def _cross_device_unsupported(
    source: Path,
    destination: Path,
) -> LoopMemoryError:
    return LoopMemoryError(
        code="migration_cross_device_unsupported",
        message=(
            "Migration source and quarantine must share a filesystem: "
            f"{source} and {destination}"
        ),
        recoverable=False,
    )


def _next_timestamp(manifest: dict[str, object]) -> float:
    return max(time.time(), float(manifest["updated_at"]))


def _inventory_sha256(files: object) -> str:
    content = json.dumps(
        files,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def load_classification_snapshot(
    path: Path,
    manifest: dict[str, object],
    loop_root: Path,
) -> ClassificationSnapshot:
    source_path = _absolute_lexical(path)
    content = _read_classification_bytes(source_path)
    _, digest = _classification_from_bytes(
        content,
        source_path,
        manifest,
        loop_root,
    )
    return ClassificationSnapshot(
        source_path=source_path,
        content=content,
        sha256=digest,
    )


def _load_classification(
    path: Path,
    manifest: dict[str, object],
    loop_root: Path,
) -> tuple[dict[str, object], str]:
    snapshot = load_classification_snapshot(path, manifest, loop_root)
    return _classification_from_snapshot(snapshot, manifest, loop_root)


def _classification_from_snapshot(
    snapshot: ClassificationSnapshot,
    manifest: dict[str, object],
    loop_root: Path,
) -> tuple[dict[str, object], str]:
    classification, digest = _classification_from_bytes(
        snapshot.content,
        snapshot.source_path,
        manifest,
        loop_root,
    )
    if digest != snapshot.sha256:
        raise _invalid_classification("classification snapshot digest is invalid")
    return classification, digest


def _classification_from_bytes(
    content: bytes,
    path: Path,
    manifest: dict[str, object],
    loop_root: Path,
) -> tuple[dict[str, object], str]:
    classification = _parse_classification_bytes(content, path)
    return _validate_classification(classification, manifest, loop_root)


def _read_classification_bytes(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise _invalid_classification("classification file is unavailable") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _invalid_classification(
            "classification file must be a regular non-symlink file"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _invalid_classification(
            "classification file could not be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise _invalid_classification(
                "classification file changed during validation"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise _invalid_classification(
                "classification file changed during validation"
            )
        content = b"".join(chunks)
        if len(content) != opened.st_size:
            raise _invalid_classification(
                "classification file changed during validation"
            )
        return content
    except OSError as error:
        raise _invalid_classification(
            "classification file could not be read safely"
        ) from error
    finally:
        os.close(descriptor)


def _validate_classification(
    classification: dict[str, object],
    manifest: dict[str, object],
    loop_root: Path,
) -> tuple[dict[str, object], str]:
    required_fields = {
        "migration_id",
        "actions",
        "reference_updates",
    }
    if not required_fields.issubset(classification) or not set(
        classification
    ).issubset(required_fields | {"approved_protected"}):
        raise _invalid_classification("classification fields are invalid")
    migration_id = classification["migration_id"]
    if not isinstance(migration_id, str) or migration_id != manifest["migration_id"]:
        raise LoopMemoryError(
            code="classification_mismatch",
            message="Classification migration ID does not match the manifest",
            recoverable=False,
        )
    approved = classification.get("approved_protected")
    if approved is not None and not isinstance(approved, bool):
        raise _invalid_classification("approved_protected must be a boolean")
    reference_updates = classification["reference_updates"]
    if not isinstance(reference_updates, list):
        raise _invalid_classification("reference_updates must be a list")
    if reference_updates:
        raise LoopMemoryError(
            code="protected_reference_update",
            message="Reference updates require an explicit external governance change",
            recoverable=False,
        )
    actions = classification["actions"]
    if not isinstance(actions, list):
        raise _invalid_classification("actions must be a list")
    inventory_paths = {
        record["relative_path"] for record in manifest["files"]
    }
    seen_sources: set[str] = set()
    destination_modes: dict[str, str] = {}
    for action in actions:
        if not isinstance(action, dict):
            raise _invalid_classification("action fields are invalid")
        action_fields = {
            "source",
            "destination",
            "mode",
        }
        project_merge = (
            manifest["source_kind"] == "project"
            and action.get("destination") == "project/project.md"
            and action.get("mode") == "merge_entries"
        )
        if project_merge:
            action_fields.add("section")
        if set(action) != action_fields:
            raise _invalid_classification("action fields are invalid")
        source = action["source"]
        destination = action["destination"]
        mode = action["mode"]
        if mode not in (
            "merge_entries",
            "copy",
            "discard_empty",
            "quarantine_only",
        ):
            raise _invalid_classification("action mode is invalid")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise _invalid_classification("action paths must be strings")
        if source in seen_sources:
            raise _invalid_classification("actions contain duplicate sources")
        previous_mode = destination_modes.get(destination)
        duplicate_destination_allowed = (
            previous_mode == "quarantine_only" and mode == "quarantine_only"
        ) or (
            manifest["source_kind"] == "project"
            and destination == "project/project.md"
            and previous_mode == "merge_entries"
            and mode == "merge_entries"
        )
        if previous_mode is not None and not duplicate_destination_allowed:
            raise _invalid_classification("actions contain duplicate destinations")
        seen_sources.add(source)
        destination_modes[destination] = mode
        if mode == "discard_empty":
            if (
                manifest["source_kind"] != "empty"
                or manifest["files"]
                or source != "."
                or destination != "discard_empty"
            ):
                raise _invalid_classification("discard_empty action is invalid")
            continue
        _validate_classification_relative_path(source, "source")
        if source not in inventory_paths:
            raise _invalid_classification("action source is not inventoried")
        if mode == "quarantine_only":
            source_kind = manifest["source_kind"]
            quarantine_allowed = (
                source_kind == "global"
                or (
                    source_kind == "project"
                    and source not in _PROJECT_AUTHORITY_SOURCES
                )
            )
            if destination != "quarantine_only" or not quarantine_allowed:
                raise _invalid_classification("quarantine_only action is invalid")
            continue
        _validate_classification_relative_path(destination, "destination")
        source_kind = manifest["source_kind"]
        if source_kind == "global":
            if destination not in {
                "global/long.md",
                "global/medium.md",
                "global/short.md",
            }:
                raise _invalid_classification("global destination is invalid")
        elif source_kind == "project":
            if destination == "project/project.md":
                pass
            elif not _is_project_archive_action(source, destination, mode):
                raise _invalid_classification("project destination is invalid")
            if mode == "merge_entries" and action["section"] not in PROJECT_SECTIONS:
                raise _invalid_classification("project merge section is invalid")
        elif source_kind == "session":
            if not destination.startswith("session_archive/"):
                raise _invalid_classification("session destination is invalid")
            _validate_classification_relative_path(
                destination.removeprefix("session_archive/"),
                "session destination",
            )
        else:
            raise _invalid_classification("empty migration can only be discarded")
        actual_destination = _classification_destination(
            loop_root,
            manifest,
            destination,
        )
        if is_reserved_product_path(actual_destination):
            raise _invalid_classification("destination is product-owned")
    if manifest["source_kind"] == "empty":
        if len(actions) != 1 or next(iter(seen_sources), None) != ".":
            raise _invalid_classification("empty migration requires discard_empty")
    elif seen_sources != inventory_paths:
        raise _invalid_classification("every inventoried file requires one action")
    _validate_global_readme_authority(manifest, actions, _invalid_classification)
    if "target_files" in manifest:
        expected_targets = sorted(set(
            _classification_destination(
                loop_root,
                manifest,
                action["destination"],
            ).relative_to(loop_root).as_posix()
            for action in actions
            if action["mode"] not in ("discard_empty", "quarantine_only")
        ))
        actual_targets = [
            record["relative_path"] for record in manifest["target_files"]
        ]
        if actual_targets != expected_targets:
            raise _corrupt_manifest(
                "target_files does not match classified destinations"
            )
    semantic = {
        "migration_id": classification["migration_id"],
        "actions": classification["actions"],
        "reference_updates": classification["reference_updates"],
    }
    return classification, hashlib.sha256(_json_bytes(semantic)).hexdigest()


def _validate_global_readme_authority(
    manifest: dict[str, object],
    actions: list[dict[str, object]],
    error_factory,
) -> None:
    inventory_paths = {
        record["relative_path"] for record in manifest["files"]
    }
    if manifest["source_kind"] != "global":
        return
    quarantine_actions = [
        action for action in actions if action["mode"] == "quarantine_only"
    ]
    if "README.md" not in inventory_paths:
        if quarantine_actions:
            raise error_factory("quarantine_only requires global README.md")
        return
    if (
        len(quarantine_actions) != 1
        or quarantine_actions[0]["source"] != "README.md"
    ):
        raise error_factory("global README.md must use quarantine_only")
    canonical_global_actions = {
        "long.md": "global/long.md",
        "medium.md": "global/medium.md",
        "short.md": "global/short.md",
    }
    for action in actions:
        if action["mode"] == "quarantine_only":
            continue
        if (
            canonical_global_actions.get(action["source"])
            != action["destination"]
            or action["mode"] != "merge_entries"
        ):
            raise error_factory(
                "global memory mappings must preserve canonical authority"
            )


def _parse_classification_bytes(
    content: bytes,
    path: Path,
) -> dict[str, object]:
    def reject_duplicate_keys(pairs):
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError(f"non-finite constant: {value}")

    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise _invalid_classification(f"classification JSON is invalid: {path}") from error
    if not isinstance(parsed, dict):
        raise _invalid_classification("classification JSON must be an object")
    return parsed


def _validate_classification_relative_path(value: str, field: str) -> None:
    if not value or "\\" in value:
        raise _invalid_classification(f"{field} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise _invalid_classification(f"{field} path is unsafe")
    if path.as_posix() != value:
        raise _invalid_classification(f"{field} path is not normalized")


def _is_project_archive_action(source: str, destination: str, mode: str) -> bool:
    if mode != "copy" or not source.startswith(("sessions/", "agents/")):
        return False
    parts = PurePosixPath(destination).parts
    if (
        len(parts) < 6
        or parts[:3] != ("session_archive", "sessions", "archive")
        or not _ARCHIVE_MONTH.fullmatch(parts[3])
        or not parts[4].startswith("s-legacy-")
    ):
        return False
    return PurePosixPath(*parts[5:]).as_posix() == source


def _classification_destination(
    loop_root: Path,
    manifest: dict[str, object],
    destination: str,
) -> Path:
    if destination.startswith("global/"):
        candidate = loop_root / destination
    elif destination == "project/project.md":
        candidate = Path(manifest["target"]) / "project.md"
    elif destination.startswith("session_archive/"):
        candidate = Path(manifest["target"]) / destination.removeprefix(
            "session_archive/"
        )
    else:
        raise _invalid_classification("destination is invalid")
    return _safe_loop_path(loop_root, candidate)


def _invalid_classification(detail: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="invalid_classification",
        message=f"Migration classification is invalid: {detail}",
        recoverable=False,
    )


def _validate_manifest(manifest: dict[str, object]) -> None:
    fields = set(manifest)
    if not _BASE_FIELDS.issubset(fields) or not fields.issubset(
        _BASE_FIELDS | _OPERATIONAL_FIELDS
    ):
        raise _corrupt_manifest("manifest fields are invalid")
    if manifest["schema_version"] not in (1, 2) or isinstance(
        manifest["schema_version"], bool
    ):
        raise _corrupt_manifest("schema_version must be 1 or 2")
    if manifest["schema_version"] == 2:
        snapshot = manifest.get("snapshot")
        digest = manifest.get("source_inventory_sha256")
        if not isinstance(snapshot, str) or not snapshot:
            raise _corrupt_manifest("schema 2 snapshot is required")
        _validate_relative_path(snapshot, "snapshot")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise _corrupt_manifest("source_inventory_sha256 is invalid")
    migration_id = manifest["migration_id"]
    if not isinstance(migration_id, str) or not _MIGRATION_ID.fullmatch(migration_id):
        raise _corrupt_manifest("migration_id is invalid")
    state = manifest["state"]
    if not isinstance(state, str) or state not in _STATE_INDEX:
        raise _corrupt_manifest("state is invalid")
    source_kind = manifest["source_kind"]
    if not isinstance(source_kind, str) or source_kind not in (
        "global",
        "project",
        "session",
        "empty",
    ):
        raise _corrupt_manifest("source_kind is invalid")
    _validate_absolute_normalized_path(manifest["source"], "source")
    if manifest["schema_version"] == 1:
        _validate_absolute_normalized_path(manifest["target"], "target")
    else:
        _validate_relative_path(manifest["target"], "target")
    if ".memory" not in Path(manifest["source"]).parts:
        raise _corrupt_manifest("source is not a legacy .memory root")
    project_id = manifest["project_id"]
    if manifest["source_kind"] == "global":
        if project_id is not None:
            raise _corrupt_manifest("global migration cannot have a project_id")
    elif not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise _corrupt_manifest("project migration must have a project_id")
    _validate_target_shape(manifest)
    catalogued_files = manifest["catalogued_files"]
    if not isinstance(catalogued_files, list):
        raise _corrupt_manifest("catalogued_files must be a list")
    _validate_file_records(manifest["files"], "files")
    source = Path(manifest["source"])
    if (
        manifest["source_kind"] == "global"
        and source != _canonical_legacy_global_root()
    ):
        raise _corrupt_manifest("global source is not the canonical legacy global root")
    if (
        state != "detected"
        and manifest["schema_version"] == 1
        and _source_kind(source, manifest["files"]) != manifest["source_kind"]
    ):
        raise _corrupt_manifest("source_kind does not match the inventory")
    for relative_path in catalogued_files:
        _validate_relative_path(relative_path, "catalogued_files")
    if len(catalogued_files) != len(set(catalogued_files)):
        raise _corrupt_manifest("catalogued_files contains duplicates")
    inventory_paths = {
        record["relative_path"] for record in manifest["files"]
    }
    if not set(catalogued_files).issubset(inventory_paths):
        raise _corrupt_manifest("catalogued_files is not a subset of files")
    for field in ("created_at", "updated_at"):
        value = manifest[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise _corrupt_manifest(f"{field} must be a nonnegative finite number")
    if manifest["updated_at"] < manifest["created_at"]:
        raise _corrupt_manifest("updated_at precedes created_at")
    warnings = manifest["warnings"]
    if not isinstance(warnings, list) or not all(
        isinstance(item, str)
        and len(item) <= 256
        and "\n" not in item
        and "\r" not in item
        for item in warnings
    ):
        raise _corrupt_manifest("warnings must be a list of short strings")
    protected = manifest.get("protected", False)
    if not isinstance(protected, bool):
        raise _corrupt_manifest("protected must be a boolean")
    protection_reasons = manifest.get("protection_reasons", [])
    if not isinstance(protection_reasons, list) or not all(
        isinstance(item, str) and item in ("catalogued_source", "credential_assignment")
        for item in protection_reasons
    ):
        raise _corrupt_manifest("protection_reasons is invalid")
    if len(protection_reasons) != len(set(protection_reasons)):
        raise _corrupt_manifest("protection_reasons contains duplicates")
    if (catalogued_files or protection_reasons) and not protected:
        raise _corrupt_manifest("protected source is not marked protected")
    if protected and not protection_reasons:
        raise _corrupt_manifest("protected source has no protection reason")
    classification_sha256 = manifest.get("classification_sha256")
    if classification_sha256 is not None and (
        not isinstance(classification_sha256, str)
        or not _HASH.fullmatch(classification_sha256)
    ):
        raise _corrupt_manifest("classification_sha256 is invalid")
    target_files = manifest.get("target_files")
    if target_files is not None:
        _validate_file_records(target_files, "target_files")
    copied_or_later = _STATE_INDEX[state] >= _STATE_INDEX["copied"]
    if copied_or_later and (
        classification_sha256 is None or target_files is None
    ):
        raise _corrupt_manifest("copied migration metadata is missing")
    if not copied_or_later and target_files is not None:
        raise _corrupt_manifest("target_files exists before copied state")
    staging_path = manifest.get("staging_path")
    publish_plan_sha256 = manifest.get("publish_plan_sha256")
    if copied_or_later:
        if manifest["schema_version"] == 1:
            _validate_absolute_normalized_path(staging_path, "staging_path")
        else:
            _validate_relative_path(staging_path, "staging_path")
        staging = Path(staging_path)
        if (
            staging.name != migration_id
            or staging.parent.name != "staging"
            or staging.parent.parent.name != "migrations"
        ):
            raise _corrupt_manifest("staging_path shape is invalid")
        if (
            not isinstance(publish_plan_sha256, str)
            or not _HASH.fullmatch(publish_plan_sha256)
        ):
            raise _corrupt_manifest("publish_plan_sha256 is invalid")
    elif staging_path is not None or publish_plan_sha256 is not None:
        raise _corrupt_manifest("staging metadata exists before copied state")
    hold_reason = manifest.get("hold_reason")
    if hold_reason is not None and (
        state != "validated" or hold_reason != "governance_switch"
    ):
        raise _corrupt_manifest("hold_reason is invalid for the current state")
    quarantine_path = manifest.get("quarantine_path")
    quarantined_or_later = _STATE_INDEX[state] >= _STATE_INDEX["quarantined"]
    if quarantined_or_later:
        if manifest["schema_version"] == 1:
            _validate_absolute_normalized_path(quarantine_path, "quarantine_path")
        else:
            _validate_relative_path(quarantine_path, "quarantine_path")
    elif quarantine_path is not None:
        raise _corrupt_manifest("quarantine_path exists before quarantine")


def _validate_file_records(value: object, field: str) -> None:
    if not isinstance(value, list):
        raise _corrupt_manifest(f"{field} must be a list")
    paths: list[str] = []
    for record in value:
        if not isinstance(record, dict) or set(record) != {
            "relative_path",
            "sha256",
            "size",
        }:
            raise _corrupt_manifest(f"{field} record is invalid")
        relative_path = record["relative_path"]
        _validate_relative_path(relative_path, field)
        paths.append(relative_path)
        digest = record["sha256"]
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise _corrupt_manifest(f"{field} hash is invalid")
        size = record["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _corrupt_manifest(f"{field} size is invalid")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise _corrupt_manifest(f"{field} paths must be sorted and unique")


def _validate_target_shape(manifest: dict[str, object]) -> None:
    target = Path(manifest["target"])
    project_id = manifest["project_id"]
    kind = manifest["source_kind"]
    if kind == "global":
        if target.name != "global":
            raise _corrupt_manifest("global target shape is invalid")
        return
    if kind in ("project", "empty"):
        if target.name != project_id or target.parent.name != "projects":
            raise _corrupt_manifest("project target shape is invalid")
        return
    expected_session = f"s-legacy-{manifest['migration_id'][2:]}"
    if (
        target.name != expected_session
        or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", target.parent.name)
        or target.parent.parent.name != "archive"
        or target.parent.parent.parent.name != "sessions"
        or target.parent.parent.parent.parent.name != project_id
        or target.parent.parent.parent.parent.parent.name != "projects"
    ):
        raise _corrupt_manifest("session target shape is invalid")


def _validate_relative_path(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _corrupt_manifest(f"{field} relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise _corrupt_manifest(f"{field} relative path is unsafe")


def _validate_absolute_normalized_path(value: object, field: str) -> None:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _corrupt_manifest(f"{field} must be an absolute path")
    if str(_absolute_lexical(Path(value))) != value:
        raise _corrupt_manifest(f"{field} path is not normalized")


def _corrupt_manifest(detail: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_state",
        message=f"Migration manifest is corrupt: {detail}",
        recoverable=False,
    )


def _refresh_not_allowed(detail: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="refresh_not_allowed",
        message=f"Migration manifest is not eligible for refresh: {detail}",
        recoverable=False,
    )


def _refresh_source_changed() -> LoopMemoryError:
    return LoopMemoryError(
        code="source_changed",
        message="Migration source no longer matches the inventoried identity",
        recoverable=False,
    )


def _refresh_migration_conflict() -> LoopMemoryError:
    return LoopMemoryError(
        code="migration_conflict",
        message="Migration manifest changed during refresh",
        recoverable=False,
    )


def _refresh_write_failed() -> LoopMemoryError:
    return LoopMemoryError(
        code="migration_write_failed",
        message="Migration manifest refresh could not be written",
        recoverable=True,
    )
