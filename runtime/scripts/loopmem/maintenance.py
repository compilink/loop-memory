from dataclasses import dataclass
from contextlib import ExitStack
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import migration as migration_module
from scripts.loopmem import registry as registry_module
from scripts.loopmem import storage as storage_module
from scripts.loopmem.paths import discover_project, is_reserved_product_path
from scripts.loopmem.registry import RegistryStore
from scripts.loopmem.sessions import session_has_unresolved_outbox
from scripts.loopmem.storage import FileLease


RETENTION_DAYS = 90

_SECONDS_PER_DAY = 24 * 60 * 60
_PROJECT_ID = re.compile(r"^p-[A-Za-z0-9][A-Za-z0-9._-]*$")
_SESSION_ID = re.compile(r"^s-[A-Za-z0-9][A-Za-z0-9._-]*$")
_ARCHIVE_MONTH = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_MIGRATION_ID = re.compile(r"^m-[0-9a-f]{32}$")
_PROTECTED_APPROVAL_WARNING = (
    "Protected legacy source requires explicit approval."
)
_CLEANUP_MARKER_PHASES = {
    "quarantine_deleting",
    "quarantine_deleted",
    "staging_deleting",
    "complete",
}
_CLEANUP_MARKER_BASE_FIELDS = {
    "schema_version",
    "migration_id",
    "manifest_sha256",
    "manifest_identity",
    "phase",
}
_CLEANUP_MARKER_ARTIFACT_FIELDS = {
    "quarantine_path",
    "quarantine_identity",
    "quarantine_mtime",
    "staging_path",
    "staging_identity",
    "staging_mtime",
}


@dataclass(frozen=True)
class _TreeSnapshot:
    path: Path
    identity: tuple[int, int]
    mtime: float


@dataclass(frozen=True)
class _SessionDeletion:
    snapshot: _TreeSnapshot
    project_id: str
    session_id: str


@dataclass(frozen=True)
class _MigrationDeletion:
    migration_id: str
    manifest_path: Path
    manifest_identity: tuple[int, int]
    manifest: dict[str, object]
    quarantine: _TreeSnapshot | None
    staging: _TreeSnapshot | None
    marker: "_CleanupMarkerSnapshot | None"


@dataclass(frozen=True)
class _CleanupMarkerSnapshot:
    path: Path
    identity: tuple[int, int]
    value: dict[str, object]


def maintain(loop_root: Path, now: float) -> dict[str, object]:
    current = _validate_now(now)
    root = _maintenance_root(loop_root)
    locks = _required_real_directory(root, root / "locks", "locks directory")
    lease_path = _internal_path(root, locks / "maintenance.lock")
    migration_lease_path = _internal_path(root, locks / "migration.lock")
    cutoff = current - RETENTION_DAYS * _SECONDS_PER_DAY

    with ExitStack() as leases:
        leases.enter_context(FileLease(lease_path, owner="maintenance"))
        leases.enter_context(
            FileLease(migration_lease_path, owner="maintenance:migration")
        )
        _validate_maintenance_registry(root)
        preserved: list[dict[str, str]] = []
        warnings: set[str] = set()
        session_deletions = _scan_sessions(root, cutoff, preserved)
        migration_deletions = _scan_migrations(
            root,
            cutoff,
            preserved,
            warnings,
        )
        for deletion in sorted(
            session_deletions,
            key=lambda item: (item.project_id, item.session_id),
        ):
            archive_lease = _internal_path(
                root,
                locks
                / f"archive-{deletion.project_id}-{deletion.session_id}.lock",
            )
            leases.enter_context(
                FileLease(
                    archive_lease,
                    owner=(
                        f"maintenance:archive:{deletion.project_id}:"
                        f"{deletion.session_id}"
                    ),
                )
            )

        _preflight_migration_deletions(root, migration_deletions, cutoff)

        deleted: list[dict[str, str]] = []
        for deletion in session_deletions:
            reason = _revalidate_session(root, deletion, cutoff)
            if reason is not None:
                preserved.append(
                    _record(
                        "archived_session",
                        deletion.session_id,
                        deletion.snapshot.path,
                        reason,
                    )
                )
                continue
            if _remove_tree(root, deletion.snapshot):
                deleted.append(
                    _record(
                        "archived_session",
                        deletion.session_id,
                        deletion.snapshot.path,
                    )
                )
            else:
                preserved.append(
                    _record(
                        "archived_session",
                        deletion.session_id,
                        deletion.snapshot.path,
                        "changed_before_delete",
                    )
                )

        for deletion in migration_deletions:
            _cleanup_migration(
                root,
                deletion,
                cutoff,
                deleted,
                preserved,
            )

    return {
        "operation": "maintain",
        "warnings": sorted(warnings),
        "deleted": _sorted_records(deleted),
        "preserved": _sorted_records(preserved),
    }


def diagnose(loop_root: Path, cwd: Path) -> dict[str, object]:
    root, root_metadata, root_usable, issues = _diagnose_root(loop_root)
    discovery = discover_project(Path(cwd))
    registry_metadata: dict[str, object] = {
        "exists": False,
        "schema_version": None,
        "integrity": "missing",
    }
    active_locks: list[str] = []
    stale_locks: list[str] = []
    incomplete_migrations: list[dict[str, object]] = []
    project_id: str | None = None
    reserved_root = any(
        issue.get("component") == "root"
        and issue.get("code") == "reserved_product_memory"
        for issue in issues
    )
    if reserved_root:
        containment: dict[str, bool | None] = {
            "registry": False,
            "locks": False,
            "manifests": False,
            "project": None,
            "migrations": False,
        }
    else:
        containment = {
            "registry": _metadata_path_contained(root, root / "registry.json"),
            "locks": _metadata_path_contained(root, root / "locks"),
            "manifests": _metadata_path_contained(
                root,
                root / "migrations/manifests",
            ),
            "project": None,
            "migrations": True,
        }

    if root_usable:
        registry_path = root / "registry.json"
        registry_metadata, registry_state = _diagnose_registry(
            root,
            registry_path,
            issues,
        )
        if registry_state is not None:
            try:
                normalized = registry_module._normalize_discovery(discovery)
                store = RegistryStore.__new__(RegistryStore)
                project_id, _ = RegistryStore._resolve_project(
                    store,
                    registry_state,
                    normalized,
                    create=False,
                )
            except LoopMemoryError as error:
                issues.append(
                    {
                        "component": "registry",
                        "code": error.code,
                    }
                )
                registry_metadata["integrity"] = "corrupt"
        active_locks, stale_locks = _diagnose_locks(root, issues)
        migration_contained, incomplete_migrations = _diagnose_migrations(
            root,
            issues,
        )
        containment["migrations"] = migration_contained

    if project_id is not None:
        containment["project"] = _metadata_path_contained(
            root,
            root / "projects" / project_id,
        )
    containment["all"] = all(value is not False for value in containment.values())

    return {
        "operation": "diagnose",
        "root": root_metadata,
        "registry": registry_metadata,
        "active_locks": sorted(active_locks),
        "stale_locks": sorted(stale_locks),
        "incomplete_migrations": sorted(
            incomplete_migrations,
            key=lambda item: (item["migration_id"], item["state"]),
        ),
        "discovery": {
            "kind": discovery.kind,
            "cwd": str(discovery.cwd),
            "root": str(discovery.root),
            "alias": discovery.alias,
            "project_id": project_id,
        },
        "containment": containment,
        "issues": _sorted_issues(issues),
    }


def doctor(loop_root: Path, cwd: Path) -> dict[str, object]:
    result = diagnose(loop_root, cwd)
    root = Path(result["root"]["path"])
    current_project_id = result["discovery"]["project_id"]
    actionable: list[dict[str, object]] = []
    for record in result["incomplete_migrations"]:
        migration_id = record["migration_id"]
        manifest_path = root / "migrations" / "manifests" / f"{migration_id}.json"
        try:
            manifest, _ = _load_manifest_snapshot(manifest_path)
        except (FileNotFoundError, LoopMemoryError):
            actionable.append(dict(record))
            continue
        source_kind = manifest["source_kind"]
        if source_kind == "global":
            blocking_scope = "global"
        elif manifest["project_id"] == current_project_id:
            blocking_scope = "current-project"
        else:
            blocking_scope = "other-project"
        actionable.append(
            {
                **record,
                "manifest": str(manifest_path),
                "source_kind": source_kind,
                "blocking_scope": blocking_scope,
                "protected": manifest.get("protected") is True,
                "next_action": {
                    "operation": "migrate-apply",
                    "requires_classification": True,
                    "requires_explicit_approval": (
                        manifest.get("protected") is True
                    ),
                },
            }
        )
    result["operation"] = "doctor"
    result["incomplete_migrations"] = actionable
    return result


def _validate_now(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise LoopMemoryError(
            code="invalid_now",
            message="Maintenance time must be a finite nonnegative number",
        )
    return float(value)


def _validate_maintenance_registry(root: Path) -> dict[str, object]:
    path = _internal_path(root, root / "registry.json")
    try:
        state, _ = _load_registry_snapshot(path)
    except FileNotFoundError as error:
        raise LoopMemoryError(
            code="corrupt_state",
            message="Maintenance registry is missing",
            recoverable=False,
        ) from error
    return state


def _maintenance_root(loop_root: Path) -> Path:
    lexical = Path(os.path.abspath(Path(loop_root).expanduser()))
    if is_reserved_product_path(lexical):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Maintenance cannot target product-owned memory",
            recoverable=False,
        )
    if lexical == Path(lexical.anchor):
        raise _unsafe("Loop root cannot be a filesystem root")
    try:
        root_stat = lexical.lstat()
    except FileNotFoundError as error:
        raise LoopMemoryError(
            code="invalid_loop_root",
            message=f"Loop root does not exist: {lexical}",
            recoverable=False,
        ) from error
    if stat.S_ISLNK(root_stat.st_mode):
        raise _unsafe(f"Loop root is a symlink: {lexical}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise _unsafe(f"Loop root is not a real directory: {lexical}")
    root = lexical.resolve(strict=True)
    if is_reserved_product_path(root):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Maintenance cannot target product-owned memory",
            recoverable=False,
        )
    return root


def _scan_sessions(
    root: Path,
    cutoff: float,
    preserved: list[dict[str, str]],
) -> list[_SessionDeletion]:
    projects = _optional_real_directory(root, root / "projects", "projects directory")
    if projects is None:
        return []
    deletions: list[_SessionDeletion] = []
    for project in _real_directory_children(root, projects, "project"):
        if not _PROJECT_ID.fullmatch(project.name):
            raise _unsafe(f"Unexpected project directory: {project}")
        sessions = _optional_real_directory(
            root,
            project / "sessions",
            "sessions directory",
        )
        if sessions is None:
            continue
        active = _optional_real_directory(
            root,
            sessions / "active",
            "active sessions directory",
        )
        if active is not None:
            for session in _real_directory_children(root, active, "active session"):
                if not _SESSION_ID.fullmatch(session.name):
                    raise _unsafe(f"Unexpected active session directory: {session}")
                preserved.append(
                    _record("active_session", session.name, session, "active")
                )

        archive = _optional_real_directory(
            root,
            sessions / "archive",
            "session archive directory",
        )
        if archive is None:
            continue
        for month in _real_directory_children(root, archive, "archive month"):
            if not _ARCHIVE_MONTH.fullmatch(month.name):
                raise _unsafe(f"Unexpected archive month directory: {month}")
            for session in _real_directory_children(root, month, "archived session"):
                if not _SESSION_ID.fullmatch(session.name):
                    raise _unsafe(f"Unexpected archived session directory: {session}")
                snapshot = _snapshot_tree(root, session)
                if snapshot.mtime >= cutoff:
                    preserved.append(
                        _record(
                            "archived_session",
                            session.name,
                            session,
                            "retention",
                        )
                    )
                elif session_has_unresolved_outbox(session):
                    preserved.append(
                        _record(
                            "archived_session",
                            session.name,
                            session,
                            "unresolved_outbox",
                        )
                    )
                else:
                    deletions.append(
                        _SessionDeletion(
                            snapshot=snapshot,
                            project_id=project.name,
                            session_id=session.name,
                        )
                    )
    return deletions


def _scan_migrations(
    root: Path,
    cutoff: float,
    preserved: list[dict[str, str]],
    warnings: set[str],
) -> list[_MigrationDeletion]:
    migrations = _optional_real_directory(
        root,
        root / "migrations",
        "migrations directory",
    )
    if migrations is None:
        return []
    try:
        ledger_events = migration_module.read_ledger_events(root)
    except LoopMemoryError as error:
        raise _maintenance_blocked("migration_ledger") from error
    ledger_ids = {event["migration_id"] for event in ledger_events}
    manifests_dir = _optional_real_directory(
        root,
        migrations / "manifests",
        "migration manifests directory",
    )
    quarantine_root = _optional_real_directory(
        root,
        migrations / "quarantine",
        "migration quarantine directory",
    )
    staging_root = _optional_real_directory(
        root,
        migrations / "staging",
        "migration staging directory",
    )
    cleanup_root = _optional_real_directory(
        root,
        migrations / "maintenance",
        "migration maintenance directory",
    )
    quarantines = _migration_tree_map(root, quarantine_root, "quarantine")
    stagings = _migration_tree_map(root, staging_root, "staging")
    markers = _cleanup_marker_map(root, cleanup_root)
    seen: set[str] = set()
    deletions: list[_MigrationDeletion] = []

    if manifests_dir is not None:
        for path in _directory_entries(manifests_dir):
            path_stat = path.lstat()
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                raise _unsafe(f"Migration manifest is not a real file: {path}")
            match = re.fullmatch(r"(m-[0-9a-f]{32})\.json", path.name)
            if match is None:
                raise _maintenance_blocked("corrupt_manifest")
            migration_id = match.group(1)
            seen.add(migration_id)
            try:
                manifest, manifest_identity = _load_manifest_snapshot(path)
            except LoopMemoryError as error:
                if error.code == "unsafe_path":
                    raise
                raise _maintenance_blocked("corrupt_manifest") from error
            if manifest["migration_id"] != migration_id:
                raise _maintenance_blocked("corrupt_manifest")
            _validate_manifest_containment(root, manifest)
            try:
                migration_module.validate_ledger_events(
                    ledger_events,
                    manifest,
                )
            except LoopMemoryError as error:
                raise _maintenance_blocked("migration_ledger") from error

            quarantine = quarantines.get(migration_id)
            staging = stagings.get(migration_id)
            if manifest["state"] != "complete":
                reason = (
                    "migration_held"
                    if manifest.get("hold_reason") is not None
                    else "migration_incomplete"
                )
                raise _maintenance_blocked(reason)
            if manifest.get("schema_version") == 2:
                # Version-two migrations retain their verified legacy snapshot
                # until an explicit legacy-delete. Periodic maintenance must
                # not reinterpret that custody as an expiring quarantine.
                _verify_complete_migration(
                    root,
                    migration_module._inflate_v2_manifest(manifest, root),
                )
                if staging is not None:
                    _preserve_migration_paths(
                        preserved,
                        migration_id,
                        None,
                        staging.path,
                        "retained_snapshot",
                    )
                continue
            expected_quarantine = (
                root / "migrations/quarantine" / migration_id / "source"
            )
            expected_staging = root / "migrations/staging" / migration_id
            marker = markers.get(migration_id)
            conflict = (
                _manifest_internal_path(root, manifest.get("quarantine_path"))
                != expected_quarantine
                or _manifest_internal_path(root, manifest.get("staging_path"))
                != expected_staging
                or _has_unresolved_manifest_warning(manifest)
            )
            if conflict:
                raise _maintenance_blocked("migration_conflicted")

            if marker is None:
                if (
                    quarantine is None
                    or not _quarantine_shape_is_exact(quarantine.path)
                ):
                    raise _maintenance_blocked("migration_conflicted")
                _verify_complete_migration(root, manifest)
                retention_mtime = quarantine.mtime
            else:
                _validate_cleanup_marker_binding(
                    root,
                    marker,
                    migration_id,
                    manifest_identity,
                    manifest,
                    quarantine,
                    staging,
                )
                if marker.value["phase"] == "complete":
                    _verify_post_quarantine_state(root, manifest)
                    continue
                if quarantine is not None:
                    _verify_marker_quarantine_state(
                        root,
                        manifest,
                        marker,
                        quarantine,
                    )
                else:
                    _verify_post_quarantine_state(root, manifest)
                retention_mtime = float(marker.value["quarantine_mtime"])

            if retention_mtime >= cutoff:
                _preserve_migration_paths(
                    preserved,
                    migration_id,
                    quarantine.path if quarantine is not None else None,
                    staging.path if staging is not None else None,
                    "retention",
                )
                continue
            deletions.append(
                _MigrationDeletion(
                    migration_id=migration_id,
                    manifest_path=path,
                    manifest_identity=manifest_identity,
                    manifest=manifest,
                    quarantine=quarantine,
                    staging=staging,
                    marker=marker,
                )
            )

    for migration_id in sorted(set(quarantines) | set(stagings)):
        if migration_id in seen:
            continue
        raise _maintenance_blocked("migration_conflicted")
    for migration_id in sorted(markers):
        if migration_id not in seen:
            raise _maintenance_blocked("cleanup_marker_conflicted")
    if ledger_ids - seen:
        raise _maintenance_blocked("migration_ledger")
    return deletions


def _revalidate_session(
    root: Path,
    deletion: _SessionDeletion,
    cutoff: float,
) -> str | None:
    current = _snapshot_tree(root, deletion.snapshot.path)
    if (
        current.identity != deletion.snapshot.identity
        or current.mtime != deletion.snapshot.mtime
    ):
        return "changed_before_delete"
    if current.mtime >= cutoff:
        return "retention"
    if session_has_unresolved_outbox(current.path):
        return "unresolved_outbox"
    return None


def _preflight_migration_deletions(
    root: Path,
    deletions: list[_MigrationDeletion],
    cutoff: float,
) -> None:
    for deletion in deletions:
        _validate_cleanup_state(root, deletion, cutoff, deletion.marker)


def _cleanup_migration(
    root: Path,
    deletion: _MigrationDeletion,
    cutoff: float,
    deleted: list[dict[str, str]],
    preserved: list[dict[str, str]],
) -> None:
    marker = deletion.marker
    quarantine, staging = _validate_cleanup_state(
        root,
        deletion,
        cutoff,
        marker,
    )
    if quarantine is not None:
        plan = _load_cleanup_publish_plan(root, deletion.manifest)
        with migration_module._promotion_leases(
            root,
            deletion.manifest,
            plan["actions"],
        ):
            quarantine, staging = _validate_cleanup_state(
                root,
                deletion,
                cutoff,
                marker,
            )
            if marker is None:
                if quarantine is None:
                    raise _maintenance_blocked("migration_inconsistent")
                marker = _write_cleanup_marker(
                    root,
                    deletion,
                    quarantine,
                    staging,
                    "quarantine_deleting",
                    None,
                )
                quarantine, staging = _validate_cleanup_state(
                    root,
                    deletion,
                    cutoff,
                    marker,
                )
            if quarantine is None:
                raise _maintenance_blocked("migration_changed")
            if not _remove_tree(root, quarantine):
                _preserve_migration_paths(
                    preserved,
                    deletion.migration_id,
                    quarantine.path,
                    staging.path if staging is not None else None,
                    "delete_failed",
                )
                return
            deleted.append(
                _record(
                    "migration_quarantine",
                    deletion.migration_id,
                    quarantine.path,
                )
            )
            if marker.value["phase"] == "quarantine_deleting":
                marker = _write_cleanup_marker_phase(
                    root,
                    marker,
                    "quarantine_deleted",
                )
    elif marker is None:
        raise _maintenance_blocked("migration_inconsistent")
    if marker.value["phase"] == "quarantine_deleting":
        marker = _write_cleanup_marker_phase(
            root,
            marker,
            "quarantine_deleted",
        )

    quarantine, staging = _validate_cleanup_state(
        root,
        deletion,
        cutoff,
        marker,
    )
    if quarantine is not None:
        raise _maintenance_blocked("cleanup_marker_conflicted")
    if staging is not None:
        if marker.value["phase"] != "staging_deleting":
            marker = _write_cleanup_marker_phase(
                root,
                marker,
                "staging_deleting",
            )
        quarantine, staging = _validate_cleanup_state(
            root,
            deletion,
            cutoff,
            marker,
        )
        if staging is None or not _remove_tree(root, staging):
            preserved.append(
                _record(
                    "migration_staging",
                    deletion.migration_id,
                    Path(marker.value["staging_path"]),
                    "delete_failed",
                )
            )
            return
        deleted.append(
            _record(
                "migration_staging",
                deletion.migration_id,
                staging.path,
            )
        )

    _validate_cleanup_state(root, deletion, cutoff, marker)
    if marker.value["phase"] != "complete":
        _write_cleanup_marker_phase(root, marker, "complete")


def _validate_cleanup_state(
    root: Path,
    deletion: _MigrationDeletion,
    cutoff: float,
    expected_marker: _CleanupMarkerSnapshot | None,
) -> tuple[_TreeSnapshot | None, _TreeSnapshot | None]:
    try:
        manifest, identity = _load_manifest_snapshot(deletion.manifest_path)
    except (FileNotFoundError, LoopMemoryError) as error:
        raise _maintenance_blocked("migration_inconsistent") from error
    if identity != deletion.manifest_identity or manifest != deletion.manifest:
        raise _maintenance_blocked("migration_changed")
    _validate_manifest_containment(root, manifest)
    if (
        manifest["state"] != "complete"
        or _has_unresolved_manifest_warning(manifest)
    ):
        raise _maintenance_blocked("migration_inconsistent")

    quarantine_path = (
        root / "migrations/quarantine" / deletion.migration_id
    )
    staging_path = root / "migrations/staging" / deletion.migration_id
    quarantine = _optional_tree_snapshot(root, quarantine_path)
    staging = _optional_tree_snapshot(root, staging_path)

    if expected_marker is None:
        marker_path = _cleanup_marker_path(root, deletion.migration_id)
        try:
            marker_path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise _maintenance_blocked("cleanup_marker_conflicted")
        if quarantine is None or deletion.quarantine is None:
            raise _maintenance_blocked("migration_inconsistent")
        _require_same_tree(quarantine, deletion.quarantine)
        if (staging is None) != (deletion.staging is None):
            raise _maintenance_blocked("migration_changed")
        if staging is not None and deletion.staging is not None:
            _require_same_tree(staging, deletion.staging)
        if quarantine.mtime >= cutoff:
            raise _maintenance_blocked("retention_changed")
        if not _quarantine_shape_is_exact(quarantine.path):
            raise _maintenance_blocked("migration_conflicted")
        _verify_complete_migration(root, manifest)
        return quarantine, staging

    marker = _load_cleanup_marker_snapshot(expected_marker.path)
    if (
        marker.identity != expected_marker.identity
        or marker.value != expected_marker.value
    ):
        raise _maintenance_blocked("cleanup_marker_changed")
    _validate_cleanup_marker_binding(
        root,
        marker,
        deletion.migration_id,
        identity,
        manifest,
        quarantine,
        staging,
    )
    if float(marker.value["quarantine_mtime"]) >= cutoff:
        raise _maintenance_blocked("retention_changed")
    if quarantine is not None:
        _verify_marker_quarantine_state(
            root,
            manifest,
            marker,
            quarantine,
        )
    else:
        _verify_post_quarantine_state(root, manifest)
    return quarantine, staging


def _require_same_tree(
    current: _TreeSnapshot,
    expected: _TreeSnapshot,
) -> None:
    if (
        current.path != expected.path
        or current.identity != expected.identity
        or current.mtime != expected.mtime
    ):
        raise _maintenance_blocked("migration_changed")


def _optional_tree_snapshot(root: Path, path: Path) -> _TreeSnapshot | None:
    try:
        return _snapshot_tree(root, path)
    except FileNotFoundError:
        return None


def _verify_complete_migration(
    root: Path,
    manifest: dict[str, object],
) -> None:
    try:
        migration_module._verify_quarantined_state(
            root,
            manifest,
            require_alias=True,
        )
    except LoopMemoryError as error:
        raise _maintenance_blocked("migration_inconsistent") from error


def _verify_marker_quarantine_state(
    root: Path,
    manifest: dict[str, object],
    marker: _CleanupMarkerSnapshot,
    quarantine: _TreeSnapshot,
) -> None:
    if marker.value["phase"] != "quarantine_deleting":
        raise _maintenance_blocked("cleanup_marker_conflicted")
    if _quarantine_shape_is_exact(quarantine.path):
        try:
            migration_module._verify_quarantined_state(
                root,
                manifest,
                require_alias=True,
            )
            return
        except LoopMemoryError:
            pass
    _verify_marker_target_and_alias(
        root,
        manifest,
    )


def _verify_post_quarantine_state(
    root: Path,
    manifest: dict[str, object],
) -> None:
    source = Path(manifest["source"])
    quarantine = _manifest_internal_path(root, manifest["quarantine_path"])
    try:
        source.lstat()
    except FileNotFoundError:
        pass
    else:
        raise _maintenance_blocked("migration_inconsistent")
    try:
        quarantine.lstat()
    except FileNotFoundError:
        pass
    else:
        raise _maintenance_blocked("migration_inconsistent")
    _verify_manifest_alias(root, manifest)


def _verify_marker_target_and_alias(
    root: Path,
    manifest: dict[str, object],
) -> None:
    source = Path(manifest["source"])
    try:
        source.lstat()
    except FileNotFoundError:
        pass
    else:
        raise _maintenance_blocked("migration_inconsistent")
    try:
        plan = _load_cleanup_publish_plan(root, manifest)
        migration_module._verify_published_plan(root, plan)
    except LoopMemoryError as error:
        raise _maintenance_blocked("migration_inconsistent") from error
    _verify_manifest_alias(root, manifest)


def _verify_manifest_alias(
    root: Path,
    manifest: dict[str, object],
) -> None:
    try:
        alias = RegistryStore(root).resolve_legacy_alias(Path(manifest["source"]))
    except LoopMemoryError as error:
        raise _maintenance_blocked("migration_inconsistent") from error
    expected = {
        "target": manifest["target"],
        "migration_id": manifest["migration_id"],
    }
    if alias != expected:
        raise _maintenance_blocked("migration_inconsistent")


def _load_cleanup_publish_plan(
    root: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    staging = _manifest_internal_path(root, manifest["staging_path"])
    plan_path = _internal_path(root, staging / "publish-plan.json")
    try:
        content, _ = _read_metadata_snapshot(plan_path, "migration publish plan")
    except (FileNotFoundError, LoopMemoryError) as error:
        raise _maintenance_blocked("migration_inconsistent") from error
    if hashlib.sha256(content).hexdigest() != manifest["publish_plan_sha256"]:
        raise _maintenance_blocked("migration_inconsistent")
    try:
        plan = migration_module._parse_publish_plan_bytes(content, plan_path)
    except LoopMemoryError as error:
        raise _maintenance_blocked("migration_inconsistent") from error
    if (
        set(plan)
        != {"migration_id", "schema_version", "classification_sha256", "actions"}
        or plan["migration_id"] != manifest["migration_id"]
        or plan["schema_version"] != 1
        or isinstance(plan["schema_version"], bool)
        or plan["classification_sha256"] != manifest["classification_sha256"]
        or not isinstance(plan["actions"], list)
    ):
        raise _maintenance_blocked("migration_inconsistent")
    return plan


def _has_unresolved_manifest_warning(manifest: dict[str, object]) -> bool:
    warnings = manifest.get("warnings", [])
    resolved = (
        manifest.get("state") == "complete"
        and manifest.get("protected") is True
    )
    return any(
        not resolved or warning != _PROTECTED_APPROVAL_WARNING
        for warning in warnings
    )


def _migration_tree_map(
    root: Path,
    parent: Path | None,
    label: str,
) -> dict[str, _TreeSnapshot]:
    if parent is None:
        return {}
    result: dict[str, _TreeSnapshot] = {}
    for child in _real_directory_children(root, parent, f"migration {label}"):
        if not _MIGRATION_ID.fullmatch(child.name):
            raise _unsafe(f"Unexpected migration {label} directory: {child}")
        result[child.name] = _snapshot_tree(root, child)
    return result


def _cleanup_marker_map(
    root: Path,
    parent: Path | None,
) -> dict[str, _CleanupMarkerSnapshot]:
    if parent is None:
        return {}
    result: dict[str, _CleanupMarkerSnapshot] = {}
    for path in _directory_entries(parent):
        match = re.fullmatch(r"(m-[0-9a-f]{32})\.json", path.name)
        if match is None:
            raise _maintenance_blocked("cleanup_marker_corrupt")
        marker = _load_cleanup_marker_snapshot(path)
        if marker.value["migration_id"] != match.group(1):
            raise _maintenance_blocked("cleanup_marker_corrupt")
        result[match.group(1)] = marker
    return result


def _cleanup_marker_path(root: Path, migration_id: str) -> Path:
    return _internal_path(
        root,
        root / "migrations/maintenance" / f"{migration_id}.json",
    )


def _load_cleanup_marker_snapshot(path: Path) -> _CleanupMarkerSnapshot:
    try:
        content, identity = _read_metadata_snapshot(path, "cleanup marker")
        value = _parse_json_object_bytes(content)
        _validate_cleanup_marker_value(value)
    except (FileNotFoundError, LoopMemoryError) as error:
        raise _maintenance_blocked("cleanup_marker_corrupt") from error
    return _CleanupMarkerSnapshot(path=path, identity=identity, value=value)


def _validate_cleanup_marker_value(value: dict[str, object]) -> None:
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in _CLEANUP_MARKER_PHASES:
        raise _maintenance_blocked("cleanup_marker_corrupt")
    required = set(_CLEANUP_MARKER_BASE_FIELDS)
    if value.get("schema_version") == 2:
        required.discard("manifest_identity")
    if phase != "complete":
        required.update(_CLEANUP_MARKER_ARTIFACT_FIELDS)
    if set(value) != required:
        raise _maintenance_blocked("cleanup_marker_corrupt")
    if value["schema_version"] not in (1, 2) or isinstance(value["schema_version"], bool):
        raise _maintenance_blocked("cleanup_marker_corrupt")
    if not isinstance(value["migration_id"], str) or not _MIGRATION_ID.fullmatch(
        value["migration_id"]
    ):
        raise _maintenance_blocked("cleanup_marker_corrupt")
    if not isinstance(value["manifest_sha256"], str) or re.fullmatch(
        r"[0-9a-f]{64}", value["manifest_sha256"]
    ) is None:
        raise _maintenance_blocked("cleanup_marker_corrupt")
    if value["schema_version"] == 1 and not _valid_json_identity(value["manifest_identity"]):
        raise _maintenance_blocked("cleanup_marker_corrupt")
    if phase == "complete":
        return
    if not isinstance(value["quarantine_path"], str) or not _valid_json_identity(
        value["quarantine_identity"]
    ):
        raise _maintenance_blocked("cleanup_marker_corrupt")
    if not _finite_number(value["quarantine_mtime"]):
        raise _maintenance_blocked("cleanup_marker_corrupt")
    staging_values = (
        value["staging_path"],
        value["staging_identity"],
        value["staging_mtime"],
    )
    if any(item is None for item in staging_values):
        if not all(item is None for item in staging_values):
            raise _maintenance_blocked("cleanup_marker_corrupt")
    elif (
        not isinstance(value["staging_path"], str)
        or not _valid_json_identity(value["staging_identity"])
        or not _finite_number(value["staging_mtime"])
    ):
        raise _maintenance_blocked("cleanup_marker_corrupt")


def _valid_json_identity(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    )


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _manifest_fingerprint(manifest: dict[str, object]) -> str:
    content = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _validate_cleanup_marker_binding(
    root: Path,
    marker: _CleanupMarkerSnapshot,
    migration_id: str,
    manifest_identity: tuple[int, int],
    manifest: dict[str, object],
    quarantine: _TreeSnapshot | None,
    staging: _TreeSnapshot | None,
) -> None:
    value = marker.value
    if (
        value["migration_id"] != migration_id
        or (
            value["schema_version"] == 1
            and value["manifest_identity"] != list(manifest_identity)
        )
        or value["manifest_sha256"] != _manifest_fingerprint(manifest)
    ):
        raise _maintenance_blocked("cleanup_marker_conflicted")
    phase = value["phase"]
    if phase == "complete":
        if quarantine is not None or staging is not None:
            raise _maintenance_blocked("cleanup_marker_conflicted")
        return

    expected_quarantine = root / "migrations/quarantine" / migration_id
    expected_staging = root / "migrations/staging" / migration_id
    if _manifest_internal_path(root, value["quarantine_path"]) != expected_quarantine:
        raise _maintenance_blocked("cleanup_marker_conflicted")
    if quarantine is not None:
        if value["quarantine_identity"] != list(quarantine.identity):
            raise _maintenance_blocked("cleanup_marker_conflicted")
        if (
            value["quarantine_mtime"] != quarantine.mtime
            and phase != "quarantine_deleting"
        ):
            raise _maintenance_blocked("cleanup_marker_conflicted")

    if phase in {"quarantine_deleted", "staging_deleting", "complete"} and (
        quarantine is not None
    ):
        raise _maintenance_blocked("cleanup_marker_conflicted")
    marker_has_staging = value["staging_path"] is not None
    if marker_has_staging:
        if _manifest_internal_path(root, value["staging_path"]) != expected_staging:
            raise _maintenance_blocked("cleanup_marker_conflicted")
        if staging is not None:
            if value["staging_identity"] != list(staging.identity):
                raise _maintenance_blocked("cleanup_marker_conflicted")
            if (
                value["staging_mtime"] != staging.mtime
                and phase != "staging_deleting"
            ):
                raise _maintenance_blocked("cleanup_marker_conflicted")
        if staging is None and phase not in {"staging_deleting", "complete"}:
            raise _maintenance_blocked("cleanup_marker_conflicted")
    elif staging is not None:
        raise _maintenance_blocked("cleanup_marker_conflicted")


def _write_cleanup_marker(
    root: Path,
    deletion: _MigrationDeletion,
    quarantine: _TreeSnapshot,
    staging: _TreeSnapshot | None,
    phase: str,
    previous: _CleanupMarkerSnapshot | None,
) -> _CleanupMarkerSnapshot:
    path = _cleanup_marker_path(root, deletion.migration_id)
    parent = path.parent
    try:
        parent.mkdir()
    except FileExistsError:
        _required_real_directory(root, parent, "migration maintenance directory")
    value: dict[str, object] = {
        "schema_version": deletion.manifest["schema_version"],
        "migration_id": deletion.migration_id,
        "manifest_sha256": _manifest_fingerprint(deletion.manifest),
        "quarantine_path": _stored_internal_path(
            root,
            quarantine.path,
            deletion.manifest["schema_version"],
        ),
        "quarantine_identity": list(quarantine.identity),
        "quarantine_mtime": quarantine.mtime,
        "staging_path": (
            _stored_internal_path(root, staging.path, deletion.manifest["schema_version"])
            if staging is not None
            else None
        ),
        "staging_identity": list(staging.identity) if staging is not None else None,
        "staging_mtime": staging.mtime if staging is not None else None,
        "phase": phase,
    }
    if deletion.manifest["schema_version"] == 1:
        value["manifest_identity"] = list(deletion.manifest_identity)
    if previous is not None:
        value = dict(previous.value)
        value["phase"] = phase
    try:
        storage_module.write_json_atomic(path, value)
    except OSError as error:
        raise _maintenance_blocked("cleanup_marker_write_failed") from error
    return _load_cleanup_marker_snapshot(path)


def _write_cleanup_marker_phase(
    root: Path,
    marker: _CleanupMarkerSnapshot,
    phase: str,
) -> _CleanupMarkerSnapshot:
    current = _load_cleanup_marker_snapshot(marker.path)
    if current.identity != marker.identity or current.value != marker.value:
        raise _maintenance_blocked("cleanup_marker_changed")
    if phase == "complete":
        value = {
            field: marker.value[field]
            for field in _CLEANUP_MARKER_BASE_FIELDS - {"phase"}
        }
        value["phase"] = "complete"
    else:
        value = dict(marker.value)
        value["phase"] = phase
    try:
        storage_module.write_json_atomic(marker.path, value)
    except OSError as error:
        raise _maintenance_blocked("cleanup_marker_write_failed") from error
    return _load_cleanup_marker_snapshot(marker.path)


def _load_manifest_snapshot(
    path: Path,
) -> tuple[dict[str, object], tuple[int, int]]:
    content, identity = _read_metadata_snapshot(path, "migration manifest")
    return _parse_manifest_bytes(content), identity


def _load_registry_snapshot(
    path: Path,
) -> tuple[dict[str, object], tuple[int, int]]:
    content, identity = _read_metadata_snapshot(path, "registry")
    value = _parse_json_object_bytes(content)
    version = value.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 2:
        raise LoopMemoryError(
            code="unsupported_schema",
            message=f"Unsupported registry schema version: {version}",
            recoverable=False,
        )
    registry_module._validate_state(value)
    return value, identity


def _manifest_internal_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise _maintenance_blocked("corrupt_manifest")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return _internal_path(root, candidate)


def _stored_internal_path(root: Path, path: Path, schema_version: object) -> str:
    path = _internal_path(root, path)
    if schema_version == 2:
        return path.relative_to(root).as_posix()
    return str(path)


def _read_metadata_snapshot(
    path: Path,
    label: str,
) -> tuple[bytes, tuple[int, int]]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _unsafe(f"{label.title()} is not a real file: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _unsafe(f"{label.title()} became a symlink: {path}") from error
        raise _corrupt_metadata(label) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _unsafe(f"{label.title()} is not a real file: {path}")
        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as error:
            raise _corrupt_metadata(label) from error
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except FileNotFoundError as error:
        raise _corrupt_metadata(label) from error
    if stat.S_ISLNK(after.st_mode):
        raise _unsafe(f"{label.title()} became a symlink: {path}")
    opened_identity = _identity(opened)
    if opened_identity != _identity(after):
        raise LoopMemoryError(
            code="corrupt_state",
            message=f"{label.title()} changed while it was read",
            recoverable=False,
        )
    return b"".join(chunks), opened_identity


def _parse_manifest_bytes(content: bytes) -> dict[str, object]:
    value = _parse_json_object_bytes(content)
    version = value.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 2:
        raise LoopMemoryError(
            code="unsupported_schema",
            message=f"Unsupported migration manifest schema: {version}",
            recoverable=False,
        )
    migration_module._validate_manifest(value)
    return value


def _parse_json_object_bytes(content: bytes) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite constant: {value}")

    def reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            content,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise _corrupt_manifest_metadata() from error
    if not isinstance(value, dict):
        raise _corrupt_manifest_metadata()
    return value


def _corrupt_manifest_metadata() -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_state",
        message="Migration manifest metadata is corrupt",
        recoverable=False,
    )


def _corrupt_metadata(label: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_state",
        message=f"{label.title()} metadata is corrupt",
        recoverable=False,
    )


def _validate_manifest_containment(
    root: Path,
    manifest: dict[str, object],
) -> None:
    for field in ("target", "staging_path", "quarantine_path"):
        value = manifest.get(field)
        if value is None:
            continue
        candidate = _manifest_internal_path(root, value)
        if not _metadata_path_contained(root, candidate):
            raise LoopMemoryError(
                code="path_outside_loop_root",
                message=f"Migration {field} is outside the loop root",
                recoverable=False,
            )


def _quarantine_shape_is_exact(path: Path) -> bool:
    try:
        entries = _directory_entries(path)
    except FileNotFoundError:
        return False
    if [entry.name for entry in entries] != ["source"]:
        return False
    try:
        source_stat = entries[0].lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(source_stat.st_mode) and not stat.S_ISLNK(source_stat.st_mode)


def _snapshot_tree(root: Path, path: Path) -> _TreeSnapshot:
    path = _internal_path(root, path)
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise _unsafe(f"Cleanup target is not a real directory: {path}")
    pending = [path]
    while pending:
        current = pending.pop()
        for entry in _directory_entries(current):
            entry_stat = entry.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                raise _unsafe(f"Cleanup tree contains a symlink: {entry}")
            if stat.S_ISDIR(entry_stat.st_mode):
                _internal_path(root, entry)
                pending.append(entry)
            elif not stat.S_ISREG(entry_stat.st_mode):
                raise _unsafe(f"Cleanup tree contains a special node: {entry}")
    return _TreeSnapshot(path=path, identity=_identity(value), mtime=value.st_mtime)


def _remove_tree(root: Path, expected: _TreeSnapshot) -> bool:
    try:
        current = _snapshot_tree(root, expected.path)
    except FileNotFoundError:
        return False
    if (
        current.identity != expected.identity
        or current.mtime != expected.mtime
    ):
        return False
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise _unsafe("Platform rmtree cannot avoid symlink attacks")
    try:
        shutil.rmtree(expected.path)
    except OSError:
        return False
    _fsync_directory(expected.path.parent)
    return True


def _required_real_directory(
    root: Path,
    path: Path,
    label: str,
) -> Path:
    result = _optional_real_directory(root, path, label)
    if result is None:
        raise LoopMemoryError(
            code="invalid_loop_root",
            message=f"Required {label} is missing: {path}",
            recoverable=False,
        )
    return result


def _optional_real_directory(
    root: Path,
    path: Path,
    label: str,
) -> Path | None:
    path = _internal_path(root, path)
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise _unsafe(f"{label.title()} is not a real directory: {path}")
    return path


def _real_directory_children(
    root: Path,
    path: Path,
    label: str,
) -> list[Path]:
    children: list[Path] = []
    for child in _directory_entries(path):
        value = child.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise _unsafe(f"{label.title()} is not a real directory: {child}")
        children.append(_internal_path(root, child))
    return children


def _directory_entries(path: Path) -> list[Path]:
    with os.scandir(path) as entries:
        return sorted((Path(entry.path) for entry in entries), key=lambda item: item.name)


def _internal_path(root: Path, candidate: Path) -> Path:
    lexical = Path(os.path.abspath(candidate))
    try:
        lexical.relative_to(root)
    except ValueError as error:
        raise LoopMemoryError(
            code="path_outside_loop_root",
            message=f"Maintenance path is outside the loop root: {lexical}",
            recoverable=False,
        ) from error
    if lexical == root:
        raise _unsafe("Loop root cannot be a cleanup target")
    resolved = lexical.resolve(strict=False)
    if resolved != lexical:
        raise _unsafe(f"Maintenance path traverses a symlink: {lexical}")
    return lexical


def _metadata_path_contained(root: Path, candidate: Path) -> bool:
    try:
        resolved = Path(candidate).expanduser().resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved != root


def _preserve_migration_paths(
    preserved: list[dict[str, str]],
    migration_id: str,
    quarantine: Path | None,
    staging: Path | None,
    reason: str,
) -> None:
    if quarantine is not None:
        preserved.append(
            _record(
                "migration_quarantine",
                migration_id,
                quarantine,
                reason,
            )
        )
    if staging is not None:
        preserved.append(
            _record(
                "migration_staging",
                migration_id,
                staging,
                reason,
            )
        )


def _record(
    kind: str,
    record_id: str,
    path: Path,
    reason: str | None = None,
) -> dict[str, str]:
    value = {"kind": kind, "id": record_id, "path": str(path)}
    if reason is not None:
        value["reason"] = reason
    return value


def _sorted_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        records,
        key=lambda item: (item["kind"], item["id"], item["path"]),
    )


def _diagnose_root(
    loop_root: Path,
) -> tuple[Path, dict[str, object], bool, list[dict[str, str]]]:
    lexical = Path(os.path.abspath(Path(loop_root).expanduser()))
    if is_reserved_product_path(lexical):
        metadata: dict[str, object] = {
            "path": str(lexical),
            "exists": False,
            "is_directory": False,
            "is_symlink": False,
            "owner_uid": None,
            "expected_owner_uid": os.getuid(),
            "owned_by_current_user": None,
            "mode": None,
        }
        return lexical, metadata, False, [
            {"component": "root", "code": "reserved_product_memory"}
        ]
    root = lexical.resolve(strict=False)
    issues: list[dict[str, str]] = []
    metadata: dict[str, object] = {
        "path": str(root),
        "exists": False,
        "is_directory": False,
        "is_symlink": False,
        "owner_uid": None,
        "expected_owner_uid": os.getuid(),
        "owned_by_current_user": None,
        "mode": None,
    }
    if is_reserved_product_path(root):
        issues.append({"component": "root", "code": "reserved_product_memory"})
        return root, metadata, False, issues
    try:
        value = lexical.lstat()
    except FileNotFoundError:
        issues.append({"component": "root", "code": "missing"})
        return root, metadata, False, issues

    is_symlink = stat.S_ISLNK(value.st_mode)
    is_directory = stat.S_ISDIR(value.st_mode) and not is_symlink
    mode = stat.S_IMODE(value.st_mode)
    metadata.update(
        {
            "exists": True,
            "is_directory": is_directory,
            "is_symlink": is_symlink,
            "owner_uid": value.st_uid,
            "owned_by_current_user": value.st_uid == os.getuid(),
            "mode": f"{mode:04o}",
        }
    )
    if is_symlink:
        issues.append({"component": "root", "code": "unsafe_path"})
    elif not is_directory:
        issues.append({"component": "root", "code": "not_directory"})
    if value.st_uid != os.getuid():
        issues.append({"component": "root", "code": "wrong_owner"})
    return root, metadata, is_directory and not is_symlink, issues


def _diagnose_registry(
    root: Path,
    path: Path,
    issues: list[dict[str, str]],
) -> tuple[dict[str, object], dict[str, object] | None]:
    metadata: dict[str, object] = {
        "exists": False,
        "schema_version": None,
        "integrity": "missing",
    }
    try:
        value = path.lstat()
    except FileNotFoundError:
        issues.append({"component": "registry", "code": "missing"})
        return metadata, None
    metadata["exists"] = True
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        metadata["integrity"] = "corrupt"
        issues.append({"component": "registry", "code": "unsafe_path"})
        return metadata, None
    try:
        state, _ = _load_registry_snapshot(path)
    except LoopMemoryError as error:
        metadata["integrity"] = (
            "unsupported_schema"
            if error.code == "unsupported_schema"
            else "corrupt"
        )
        issues.append({"component": "registry", "code": error.code})
        return metadata, None
    metadata["schema_version"] = state["schema_version"]
    metadata["integrity"] = "ok"
    return metadata, state


def _diagnose_locks(
    root: Path,
    issues: list[dict[str, str]],
) -> tuple[list[str], list[str]]:
    path = root / "locks"
    try:
        value = path.lstat()
    except FileNotFoundError:
        return [], []
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        issues.append({"component": "locks", "code": "unsafe_path"})
        return [], []
    result: list[str] = []
    stale: list[str] = []
    for entry in _directory_entries(path):
        entry_stat = entry.lstat()
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
            issues.append(
                {
                    "component": "locks",
                    "code": "unsafe_path",
                    "id": entry.name,
                }
            )
            continue
        if not entry.name.endswith(".lock"):
            continue
        try:
            content, _ = _read_metadata_snapshot(entry, "lease")
            lease = _parse_json_object_bytes(content)
            storage_module._validate_lease(entry, lease)
        except (FileNotFoundError, LoopMemoryError) as error:
            issues.append(
                {
                    "component": "locks",
                    "code": (
                        error.code
                        if isinstance(error, LoopMemoryError)
                        else "corrupt_state"
                    ),
                    "id": entry.name,
                }
            )
            continue
        if (
            lease["expires_at"] > time.time()
            or storage_module.pid_is_alive(lease["pid"])
        ):
            result.append(entry.name)
        else:
            stale.append(entry.name)
    return result, stale


def _diagnose_migrations(
    root: Path,
    issues: list[dict[str, str]],
) -> tuple[bool, list[dict[str, object]]]:
    manifest_ids: set[str] = set()
    manifest_snapshots: dict[
        str,
        tuple[dict[str, object], tuple[int, int]],
    ] = {}
    incomplete: list[dict[str, object]] = []
    try:
        ledger_events = migration_module.read_ledger_events(root)
    except LoopMemoryError as error:
        ledger_ids: set[str] | None = None
        issues.append(
            {"component": "migration_ledger", "code": error.code}
        )
    else:
        ledger_ids = {event["migration_id"] for event in ledger_events}
    manifests = root / "migrations/manifests"
    try:
        value = manifests.lstat()
    except FileNotFoundError:
        if ledger_ids is not None:
            for migration_id in sorted(ledger_ids):
                issues.append(
                    {
                        "component": "migration_ledger",
                        "code": "ledger_only",
                        "id": migration_id,
                    }
                )
        _diagnose_cleanup_markers(
            root,
            manifest_ids,
            manifest_snapshots,
            incomplete,
            issues,
        )
        return True, incomplete
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        issues.append(
            {"component": "migration_manifest", "code": "unsafe_path"}
        )
        _diagnose_cleanup_markers(
            root,
            manifest_ids,
            manifest_snapshots,
            incomplete,
            issues,
        )
        return False, incomplete
    contained = True
    for path in _directory_entries(manifests):
        match = re.fullmatch(r"(m-[0-9a-f]{32})\.json", path.name)
        migration_id = match.group(1) if match is not None else None
        if migration_id is not None:
            manifest_ids.add(migration_id)
        try:
            path_stat = path.lstat()
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                raise _unsafe("Migration manifest is not a real file")
            manifest, manifest_identity = _load_manifest_snapshot(path)
        except LoopMemoryError as error:
            issue = {
                "component": "migration_manifest",
                "code": error.code,
            }
            if migration_id is not None:
                issue["id"] = migration_id
            issues.append(issue)
            continue
        actual_id = manifest["migration_id"]
        if migration_id != actual_id:
            issues.append(
                {
                    "component": "migration_manifest",
                    "code": "corrupt_state",
                    "id": migration_id or actual_id,
                }
            )
            continue
        if ledger_ids is not None:
            try:
                migration_module.validate_ledger_events(
                    ledger_events,
                    manifest,
                )
            except LoopMemoryError as error:
                issues.append(
                    {
                        "component": "migration_ledger",
                        "code": error.code,
                        "id": actual_id,
                    }
                )
        manifest_snapshots[actual_id] = (manifest, manifest_identity)
        try:
            manifest_contained = all(
                _metadata_path_contained(
                    root,
                    _manifest_internal_path(root, value),
                )
                for field in ("target", "staging_path", "quarantine_path")
                if (value := manifest.get(field)) is not None
            )
        except LoopMemoryError as error:
            if error.code != "path_outside_loop_root":
                raise
            manifest_contained = False
        if not manifest_contained:
            contained = False
            issues.append(
                {
                    "component": "migration_manifest",
                    "code": "path_outside_loop_root",
                    "id": actual_id,
                }
            )
        if manifest["state"] != "complete":
            incomplete.append(
                {
                    "migration_id": actual_id,
                    "state": manifest["state"],
                    "hold": manifest.get("hold_reason"),
                }
            )
    if ledger_ids is not None:
        for migration_id in sorted(ledger_ids - manifest_ids):
            issues.append(
                {
                    "component": "migration_ledger",
                    "code": "ledger_only",
                    "id": migration_id,
                }
            )
    _diagnose_cleanup_markers(
        root,
        manifest_ids,
        manifest_snapshots,
        incomplete,
        issues,
    )
    return contained, incomplete


def _diagnose_cleanup_markers(
    root: Path,
    manifest_ids: set[str],
    manifest_snapshots: dict[
        str,
        tuple[dict[str, object], tuple[int, int]],
    ],
    incomplete: list[dict[str, object]],
    issues: list[dict[str, str]],
) -> None:
    marker_dir = root / "migrations/maintenance"
    try:
        directory_stat = marker_dir.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
        directory_stat.st_mode
    ):
        issues.append(
            {"component": "migration_cleanup", "code": "unsafe_path"}
        )
        return

    for path in _directory_entries(marker_dir):
        match = re.fullmatch(r"(m-[0-9a-f]{32})\.json", path.name)
        migration_id = match.group(1) if match is not None else None
        try:
            path_stat = path.lstat()
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(
                path_stat.st_mode
            ):
                raise _unsafe("Cleanup marker is not a real file")
            marker = _load_cleanup_marker_snapshot(path)
        except (FileNotFoundError, LoopMemoryError) as error:
            issue = {
                "component": "migration_cleanup",
                "code": (
                    "unsafe_path"
                    if isinstance(error, LoopMemoryError)
                    and error.code == "unsafe_path"
                    else "corrupt_state"
                ),
            }
            if migration_id is not None:
                issue["id"] = migration_id
            issues.append(issue)
            continue

        marker_id = marker.value["migration_id"]
        if migration_id != marker_id:
            issues.append(
                {
                    "component": "migration_cleanup",
                    "code": "inconsistent_marker",
                    "id": migration_id or marker_id,
                }
            )
            continue
        phase = marker.value["phase"]
        if phase != "complete":
            incomplete.append(
                {
                    "migration_id": marker_id,
                    "state": f"cleanup:{phase}",
                    "hold": None,
                }
            )
        if marker_id not in manifest_ids:
            issues.append(
                {
                    "component": "migration_cleanup",
                    "code": "orphaned_marker",
                    "id": marker_id,
                }
            )
            continue
        manifest_snapshot = manifest_snapshots.get(marker_id)
        if manifest_snapshot is None:
            issues.append(
                {
                    "component": "migration_cleanup",
                    "code": "inconsistent_marker",
                    "id": marker_id,
                }
            )
            continue
        manifest, manifest_identity = manifest_snapshot
        try:
            quarantine = _optional_tree_snapshot(
                root,
                root / "migrations/quarantine" / marker_id,
            )
            staging = _optional_tree_snapshot(
                root,
                root / "migrations/staging" / marker_id,
            )
            _validate_cleanup_marker_binding(
                root,
                marker,
                marker_id,
                manifest_identity,
                manifest,
                quarantine,
                staging,
            )
        except (FileNotFoundError, LoopMemoryError):
            issues.append(
                {
                    "component": "migration_cleanup",
                    "code": "inconsistent_marker",
                    "id": marker_id,
                }
            )


def _sorted_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for issue in issues:
        key = (
            issue["component"],
            issue["code"],
            issue.get("id", ""),
        )
        unique[key] = issue
    return sorted(
        unique.values(),
        key=lambda item: (
            item["component"],
            item["code"],
            item.get("id", ""),
        ),
    )


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unsafe(message: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="unsafe_path",
        message=message,
        recoverable=False,
    )


def _maintenance_blocked(reason: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="maintenance_blocked",
        message=f"Maintenance requires diagnosis or recovery: {reason}",
        recoverable=False,
    )
