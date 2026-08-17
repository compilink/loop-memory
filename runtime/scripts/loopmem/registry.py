from collections.abc import Callable
import os
from pathlib import Path
import re
import stat
import uuid

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.paths import (
    ProjectDiscovery,
    assert_loop_path,
    is_reserved_product_path,
)
from scripts.loopmem.storage import (
    FileLease,
    ensure_private_dir,
    read_json,
    write_json_atomic,
)


_INITIAL_STATE: dict[str, object] = {
    "schema_version": 1,
    "projects": {},
    "sessions": {},
    "legacy_aliases": {},
    "maintenance": {},
}

# Version 1 records are deliberately metadata-only: project alias lists, session
# identity pairs, and legacy migration pointers. Memory body text never belongs here.
_ROOT_FIELDS = frozenset(_INITIAL_STATE)
_PROJECT_FIELDS = frozenset(("kind", "shared_roots", "roots", "upstream_refs"))
_SESSION_FIELDS = frozenset(("project_id", "thread_id"))
_V2_ROOT_FIELDS = frozenset(("schema_version", "generation", "projects", "sessions", "legacy_aliases", "maintenance"))
_V2_PROJECT_FIELDS = frozenset(("roots",))
_V2_SESSION_FIELDS = frozenset(("project_id", "platform_session_id", "generation", "resumes_from", "state"))
_LEGACY_FIELDS = frozenset(("target", "migration_id"))
_ID_PATTERN = re.compile(r"^[ps]-[A-Za-z0-9][A-Za-z0-9._-]*$")


class RegistryStore:
    def __init__(
        self,
        loop_root: Path,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.loop_root = Path(loop_root).expanduser().resolve(strict=False)
        self.registry_path = assert_loop_path(
            self.loop_root,
            self.loop_root / "registry.json",
        )
        self.lease_path = assert_loop_path(
            self.loop_root,
            self.loop_root / "locks" / "registry.lock",
        )
        self._id_factory = id_factory

    def initialize(self) -> None:
        ensure_private_dir(self.loop_root)
        ensure_private_dir(self.lease_path.parent)
        with self._mutation_lease():
            if self.registry_path.exists():
                self._read_state()
            else:
                write_json_atomic(self.registry_path, _INITIAL_STATE)

    def initialize_v2(self) -> None:
        ensure_private_dir(self.loop_root)
        ensure_private_dir(self.lease_path.parent)
        with self._mutation_lease():
            if self.registry_path.exists():
                self._read_state()
                return
            write_json_atomic(
                self.registry_path,
                {
                    "schema_version": 2,
                    "generation": 1,
                    "projects": {},
                    "sessions": {},
                    "legacy_aliases": {},
                    "maintenance": {},
                },
            )

    def validate(self, minimum_generation: int | None = None) -> None:
        state = self._read_state()
        if minimum_generation is not None:
            generation = state.get("generation", 0)
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < minimum_generation
            ):
                raise LoopMemoryError(
                    code="registry_generation_regressed",
                    message="Registry generation regressed",
                    recoverable=False,
                )

    def resolve_project(
        self,
        discovery: ProjectDiscovery,
        create: bool,
    ) -> str | None:
        normalized = _normalize_discovery(discovery)
        if create:
            with self._mutation_lease():
                state = self._read_state()
                project_id, changed = self._resolve_project(
                    state,
                    normalized,
                    create=True,
                )
                if changed:
                    _bump_generation(state)
                    write_json_atomic(self.registry_path, state)
                return project_id

        state = self._read_state()
        project_id, _ = self._resolve_project(
            state,
            normalized,
            create=False,
        )
        return project_id

    def resolve_session(
        self,
        project_id: str,
        thread_id: str | None,
        create: bool,
    ) -> str | None:
        info = self.resolve_session_info(project_id, thread_id, create=create)
        return info["session_id"] if info is not None else None

    def resolve_session_info(
        self,
        project_id: str,
        platform_session_id: str | None,
        create: bool,
        materialize_active: Callable[[str], Callable[[], None] | None] | None = None,
    ) -> dict[str, object] | None:
        """Resolve a session and return generation/resume metadata.

        This is the generation-aware API used by the enter orchestrator;
        ``resolve_session`` remains the compatibility ID-only API.
        """
        if not isinstance(project_id, str) or not project_id:
            raise LoopMemoryError(code="unknown_project", message="Session project ID must identify a registered project")
        if platform_session_id is not None and (not isinstance(platform_session_id, str) or not platform_session_id.strip()):
            raise LoopMemoryError(code="invalid_thread_id", message="Host thread ID must be a nonempty string or None")
        if create:
            with self._mutation_lease():
                state = self._read_state()
                info, changed = self._resolve_session_info(
                    state,
                    project_id,
                    platform_session_id,
                    create=True,
                    recover_missing_active=materialize_active is not None,
                )
                if changed:
                    created_tree: tuple[Path, tuple[int, int]] | None = None
                    rollback_materialization: Callable[[], None] | None = None
                    if materialize_active is not None:
                        session_id = str(info["session_id"])
                        active_path = self._session_active_path(project_id, session_id)
                        existed_before = _safe_real_directory(
                            self.loop_root, active_path
                        )
                        rollback_materialization = materialize_active(session_id)
                        if not existed_before:
                            value = os.lstat(active_path)
                            if not stat.S_ISDIR(value.st_mode):
                                raise _ambiguous_session(
                                    "materialized session is not a real directory"
                                )
                            created_tree = (
                                active_path,
                                (value.st_dev, value.st_ino),
                            )
                    try:
                        _bump_generation(state)
                        write_json_atomic(self.registry_path, state)
                    except BaseException as primary:
                        if created_tree is not None:
                            try:
                                if rollback_materialization is not None:
                                    rollback_materialization()
                                else:
                                    _rollback_created_tree(*created_tree)
                            except BaseException as cleanup_error:
                                raise primary from cleanup_error
                        raise primary
                return info
        state = self._read_state()
        info, _ = self._resolve_session_info(
            state,
            project_id,
            platform_session_id,
            create=False,
            recover_missing_active=False,
        )
        return info

    def mark_session_archived(self, project_id: str, session_id: str) -> None:
        """Commit the metadata half of a session-close transaction."""
        with self._mutation_lease():
            state = self._read_state()
            sessions = state["sessions"]
            if not isinstance(sessions, dict):
                raise _corrupt_state("sessions must be an object")
            record = sessions.get(session_id)
            if not isinstance(record, dict) or record.get("project_id") != project_id:
                raise LoopMemoryError(
                    code="session_not_found",
                    message="Session registry record does not exist",
                )
            if state.get("schema_version") != 2 or record.get("state") == "archived":
                return
            archives = _archive_paths(self.loop_root, project_id, session_id)
            if _safe_real_directory(
                self.loop_root,
                self._session_active_path(project_id, session_id),
            ) or len(archives) != 1:
                raise _ambiguous_session("archive commit lacks unique archive evidence")
            record["state"] = "archived"
            _bump_generation(state)
            write_json_atomic(self.registry_path, state)

    def add_legacy_alias(
        self,
        legacy_path: Path,
        target: str,
        migration_id: str,
    ) -> None:
        key = _normalize_legacy_path(legacy_path)
        if not isinstance(target, str) or not target:
            raise LoopMemoryError(
                code="invalid_legacy_alias",
                message="Legacy alias target must be a nonempty string",
            )
        if not isinstance(migration_id, str) or not migration_id:
            raise LoopMemoryError(
                code="invalid_legacy_alias",
                message="Legacy alias migration ID must be a nonempty string",
            )

        with self._mutation_lease():
            state = self._read_state()
            aliases = state["legacy_aliases"]
            if not isinstance(aliases, dict):
                raise _corrupt_state("legacy_aliases must be an object")
            schema_v2 = state.get("schema_version") == 2
            desired_target = (
                _normalize_legacy_target(self.loop_root, target)
                if schema_v2
                else target
            )
            desired = {"target": desired_target, "migration_id": migration_id}
            existing = aliases.get(key)
            if existing is not None:
                existing_target = existing.get("target")
                existing_migration = existing.get("migration_id")
                normalized_existing = existing_target
                if schema_v2:
                    try:
                        normalized_existing = _normalize_legacy_target(
                            self.loop_root, existing_target
                        )
                    except (AttributeError, LoopMemoryError, TypeError):
                        normalized_existing = None
                if (
                    existing_migration != migration_id
                    or normalized_existing != desired_target
                ):
                    raise LoopMemoryError(
                        code="legacy_alias_conflict",
                        message=f"Legacy alias already has a different target: {key}",
                        recoverable=False,
                    )
                if existing != desired:
                    aliases[key] = desired
                    _bump_generation(state)
                    write_json_atomic(self.registry_path, state)
                return
            aliases[key] = desired
            _bump_generation(state)
            write_json_atomic(self.registry_path, state)

    def resolve_legacy_alias(self, legacy_path: Path) -> dict[str, str] | None:
        key = _normalize_legacy_path(legacy_path)
        state = self._read_state()
        aliases = state["legacy_aliases"]
        if not isinstance(aliases, dict):
            raise _corrupt_state("legacy_aliases must be an object")
        record = aliases.get(key)
        if record is None:
            return None
        if not isinstance(record, dict):
            raise _corrupt_state("legacy alias record must be an object")
        target = record.get("target")
        migration_id = record.get("migration_id")
        if not isinstance(target, str) or not isinstance(migration_id, str):
            raise _corrupt_state("legacy alias record fields must be strings")
        if state.get("schema_version") == 2 and not Path(target).is_absolute():
            target = str(assert_loop_path(self.loop_root, self.loop_root / target))
        return {"target": target, "migration_id": migration_id}

    def _resolve_project(
        self,
        state: dict[str, object],
        discovery: ProjectDiscovery,
        *,
        create: bool,
    ) -> tuple[str | None, bool]:
        projects = state["projects"]
        if not isinstance(projects, dict):
            raise _corrupt_state("projects must be an object")

        root = str(discovery.root)
        schema_version = state.get("schema_version")
        if schema_version == 2:
            project_id = _unique_project_match(projects, "roots", root)
            if project_id is None and discovery.alias is not None:
                project_id = _unique_project_match(projects, "roots", discovery.alias)
            if project_id is not None:
                if not create:
                    return project_id, False
                record = projects[project_id]
                if not isinstance(record, dict):
                    raise _corrupt_state("project record must be an object")
                roots = record.get("roots")
                if not isinstance(roots, list):
                    raise _corrupt_state("project roots must be a list")
                if root not in roots:
                    roots.append(root)
                    return project_id, True
                return project_id, False
            if not create:
                return None, False
            project_id = self._new_id("p", projects)
            projects[project_id] = {"roots": [root]}
            return project_id, True
        shared_root = None
        upstream_ref = None

        common_match: str | None = None
        root_match = _unique_project_match(projects, "roots", root)
        if (
            common_match is not None
            and root_match is not None
            and common_match != root_match
        ):
            raise LoopMemoryError(
                code="ambiguous_project",
                message="Project common directory and root identify different records",
                recoverable=False,
            )

        project_id = common_match or root_match
        if project_id is not None:
            record = projects[project_id]
            if not isinstance(record, dict):
                raise _corrupt_state("project record must be an object")
            if record.get("kind") != discovery.kind:
                raise LoopMemoryError(
                    code="project_identity_conflict",
                    message=(
                        "Authoritative project alias resolves a different project kind: "
                        f"{project_id}"
                    ),
                    recoverable=False,
                )
            if not create:
                return project_id, False
            changed = _add_discovery_aliases(record, discovery)
            return project_id, changed

        if not create:
            return None, False

        project_id = self._new_id("p", projects)
        projects[project_id] = {
            "kind": discovery.kind,
            "shared_roots": [shared_root] if shared_root is not None else [],
            "roots": [root],
            "upstream_refs": [upstream_ref] if upstream_ref is not None else [],
        }
        return project_id, True

    def _new_id(self, prefix: str, records: dict[str, object]) -> str:
        suffix = self._id_factory() if self._id_factory is not None else uuid.uuid4().hex
        if not isinstance(suffix, str) or not suffix:
            raise LoopMemoryError(
                code="invalid_id",
                message="Generated registry ID suffix must be a nonempty string",
            )
        record_id = f"{prefix}-{suffix}"
        if not _ID_PATTERN.fullmatch(record_id):
            raise LoopMemoryError(
                code="invalid_id",
                message=f"Generated registry ID is invalid: {record_id!r}",
            )
        if record_id in records:
            raise LoopMemoryError(
                code="id_collision",
                message=f"Generated registry ID already exists: {record_id}",
            )
        return record_id

    def _resolve_session(
        self,
        state: dict[str, object],
        project_id: str,
        thread_id: str | None,
        *,
        create: bool,
    ) -> tuple[str | None, bool]:
        projects = state["projects"]
        sessions = state["sessions"]
        if not isinstance(projects, dict) or project_id not in projects:
            raise LoopMemoryError(
                code="unknown_project",
                message=f"Session project is not registered: {project_id}",
                recoverable=False,
            )
        if not isinstance(sessions, dict):
            raise _corrupt_state("sessions must be an object")

        platform_field = (
            "platform_session_id"
            if state.get("schema_version") == 2
            else "thread_id"
        )
        if thread_id is not None:
            matches: list[str] = []
            for session_id, record in sessions.items():
                if not isinstance(record, dict):
                    raise _corrupt_state("session record must be an object")
                if (
                    record.get("project_id") == project_id
                    and record.get(platform_field) == thread_id
                ):
                    matches.append(session_id)
            if len(matches) > 1:
                raise LoopMemoryError(
                    code="ambiguous_session",
                    message=(
                        "Host thread maps to multiple sessions in project: "
                        f"{project_id}/{thread_id}"
                    ),
                    recoverable=False,
                )
            if matches:
                return matches[0], False

        if not create:
            return None, False

        session_id = self._new_id("s", sessions)
        if state.get("schema_version") == 2:
            sessions[session_id] = {
                "project_id": project_id,
                "platform_session_id": thread_id,
                "generation": 1,
                "resumes_from": None,
                "state": "active",
            }
        else:
            sessions[session_id] = {
                "project_id": project_id,
                "thread_id": thread_id,
            }
        return session_id, True

    def _resolve_session_info(
        self,
        state: dict[str, object],
        project_id: str,
        platform_session_id: str | None,
        *,
        create: bool,
        recover_missing_active: bool,
    ) -> tuple[dict[str, object] | None, bool]:
        projects = state["projects"]
        sessions = state["sessions"]
        if not isinstance(projects, dict) or project_id not in projects:
            raise LoopMemoryError(code="unknown_project", message=f"Session project is not registered: {project_id}", recoverable=False)
        if not isinstance(sessions, dict):
            raise _corrupt_state("sessions must be an object")
        if state.get("schema_version") != 2:
            session_id, changed = self._resolve_session(state, project_id, platform_session_id, create=create)
            return ({"session_id": session_id} if session_id is not None else None), changed

        matches: list[tuple[str, dict[str, object]]] = []
        if platform_session_id is not None:
            for session_id, record in sessions.items():
                if not isinstance(record, dict):
                    raise _corrupt_state("session record must be an object")
                if record.get("project_id") == project_id and record.get("platform_session_id") == platform_session_id:
                    matches.append((session_id, record))
        if matches:
            matches.sort(key=lambda pair: int(pair[1]["generation"]), reverse=True)
            archive_evidence, missing_active = self._validate_lineage_evidence(
                project_id,
                matches,
                require_active_tree=create,
                recover_missing_active=recover_missing_active,
            )
            active = [(sid, rec) for sid, rec in matches if rec.get("state") == "active"]
            if len(active) > 1:
                raise _ambiguous_session("multiple active generations exist")
            if active:
                sid, rec = active[0]
                archives = _archive_paths(self.loop_root, project_id, sid)
                active_path = self._session_active_path(project_id, sid)
                active_exists = _safe_real_directory(self.loop_root, active_path)
                if active_exists and archives:
                    raise _ambiguous_session("active and archived trees coexist")
                if active_exists or not archives:
                    resumes_from = rec.get("resumes_from")
                    predecessor_archive = (
                        archive_evidence.get(resumes_from)
                        if isinstance(resumes_from, str)
                        else None
                    )
                    recovered = create and missing_active and not active_exists and not archives
                    return _session_info(
                        sid,
                        rec,
                        predecessor_archive,
                        session_recovered=recovered,
                    ), recovered
                # The active registry record has disappeared from active/ but
                # exactly one archive remains: reconcile then create successor.
                rec["state"] = "archived"
                predecessor_id, predecessor = sid, rec
                archive_path = archives[0]
            else:
                predecessor_id, predecessor = matches[0]
                archives = _archive_paths(self.loop_root, project_id, predecessor_id)
                if len(archives) != 1:
                    raise _ambiguous_session("archived session evidence is missing or duplicated")
                archive_path = archives[0]
            if not create:
                return _session_info(predecessor_id, predecessor, archive_path), False
            successor_id = self._new_id("s", sessions)
            successor = {
                "project_id": project_id,
                "platform_session_id": platform_session_id,
                "generation": int(predecessor["generation"]) + 1,
                "resumes_from": predecessor_id,
                "state": "active",
            }
            sessions[successor_id] = successor
            return _session_info(successor_id, successor, archive_path), True
        if not create:
            return None, False
        registered = {
            session_id
            for session_id, record in sessions.items()
            if isinstance(record, dict) and record.get("project_id") == project_id
        }
        active_ids = _active_session_ids(self.loop_root, project_id)
        if active_ids - registered:
            raise _ambiguous_session(
                "unregistered active session evidence requires review"
            )
        session_id = self._new_id("s", sessions)
        record = {
            "project_id": project_id,
            "platform_session_id": platform_session_id,
            "generation": 1,
            "resumes_from": None,
            "state": "active",
        }
        sessions[session_id] = record
        return _session_info(session_id, record), True

    def _session_active_path(self, project_id: str, session_id: str) -> Path:
        return self.loop_root / "projects" / project_id / "sessions" / "active" / session_id

    def _validate_lineage_evidence(
        self,
        project_id: str,
        matches: list[tuple[str, dict[str, object]]],
        *,
        require_active_tree: bool,
        recover_missing_active: bool,
    ) -> tuple[dict[str, Path], bool]:
        """Validate every generation's physical tree without mutating it."""
        archives: dict[str, Path] = {}
        missing_active = False
        for session_id, record in matches:
            active_exists = _safe_real_directory(
                self.loop_root,
                self._session_active_path(project_id, session_id)
            )
            archived = _archive_paths(self.loop_root, project_id, session_id)
            if record.get("state") == "archived":
                if active_exists or len(archived) != 1:
                    raise _ambiguous_session(
                        "archived generation lacks exactly one archive tree"
                    )
                archives[session_id] = archived[0]
                continue
            if active_exists and archived:
                raise _ambiguous_session("active and archived trees coexist")
            if len(archived) > 1:
                raise _ambiguous_session("active generation has multiple archives")
            # ID-only reads tolerate an unmaterialized legacy fixture. Enter
            # may recreate only an active record that has no tree or archive;
            # callers without a materializer still fail closed.
            if require_active_tree and not active_exists and not archived:
                if not recover_missing_active:
                    raise _ambiguous_session("active generation tree is missing")
                missing_active = True
        return archives, missing_active

    def _read_state(self) -> dict[str, object]:
        try:
            state = read_json(self.registry_path)
        except FileNotFoundError as error:
            raise _corrupt_state("registry file is missing") from error
        _validate_state(state)
        return state

    def _mutation_lease(self) -> FileLease:
        return FileLease(self.lease_path, "registry")


def _session_info(
    session_id: str,
    record: dict[str, object],
    archive_path: Path | None = None,
    *,
    session_recovered: bool = False,
) -> dict[str, object]:
    info: dict[str, object] = {
        "session_id": session_id,
        "session_generation": record.get("generation", 1),
        "resumes_from": record.get("resumes_from"),
        "resume_handoff": None,
    }
    if session_recovered:
        info["session_recovered"] = True
    if archive_path is not None:
        info["resume_handoff"] = str(archive_path / "handoff.md")
    return info


def _archive_paths(loop_root: Path, project_id: str, session_id: str) -> list[Path]:
    root = Path(loop_root).absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    paths: list[Path] = []
    try:
        try:
            descriptor = os.open(root, flags)
        except FileNotFoundError:
            return []
        descriptors.append(descriptor)
        archive_root = root
        for component in ("projects", project_id, "sessions", "archive"):
            try:
                descriptor = os.open(
                    component, flags, dir_fd=descriptors[-1]
                )
            except FileNotFoundError:
                return []
            except OSError as error:
                raise _ambiguous_session(
                    "archive lineage changed or contains an unsafe component"
                ) from error
            descriptors.append(descriptor)
            archive_root = archive_root / component
        archive_descriptor = descriptors[-1]
        for month_name in os.listdir(archive_descriptor):
            if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month_name):
                continue
            try:
                month_descriptor = os.open(
                    month_name, flags, dir_fd=archive_descriptor
                )
            except FileNotFoundError:
                raise _ambiguous_session("archive month changed during validation")
            except OSError as error:
                raise _ambiguous_session("archive month is unsafe") from error
            descriptors.append(month_descriptor)
            try:
                candidate_descriptor = os.open(
                    session_id, flags, dir_fd=month_descriptor
                )
            except FileNotFoundError:
                descriptors.pop()
                os.close(month_descriptor)
                continue
            except OSError as error:
                raise _ambiguous_session("archived session is unsafe") from error
            descriptors.append(candidate_descriptor)
            candidate_stat = os.fstat(candidate_descriptor)
            if not stat.S_ISDIR(candidate_stat.st_mode):
                raise _ambiguous_session("archived session is not a directory")
            paths.append((archive_root / month_name / session_id).absolute())
            descriptors.pop()
            os.close(candidate_descriptor)
            descriptors.pop()
            os.close(month_descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if len(paths) > 1:
        raise _ambiguous_session("multiple archive trees exist")
    return paths


def _active_session_ids(loop_root: Path, project_id: str) -> set[str]:
    root = Path(loop_root).absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        try:
            descriptor = os.open(root, flags)
        except FileNotFoundError:
            return set()
        descriptors.append(descriptor)
        for component in ("projects", project_id, "sessions", "active"):
            try:
                descriptor = os.open(component, flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                return set()
            except OSError as error:
                raise _ambiguous_session("active session lineage is unsafe") from error
            descriptors.append(descriptor)
        active_descriptor = descriptors[-1]
        session_ids: set[str] = set()
        for name in os.listdir(active_descriptor):
            if not _valid_id(name, "s"):
                raise _ambiguous_session("active session directory has an invalid identity")
            try:
                child = os.open(name, flags, dir_fd=active_descriptor)
            except OSError as error:
                raise _ambiguous_session("active session entry is unsafe") from error
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise _ambiguous_session("active session entry is not a directory")
            finally:
                os.close(child)
            session_ids.add(name)
        return session_ids
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _real_directory(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(value.st_mode):
        raise _ambiguous_session(f"session evidence is not a real directory: {path}")
    return True


def _safe_real_directory(root: Path, path: Path) -> bool:
    """Validate a directory by lexical component without following symlinks."""
    root = Path(root).absolute()
    lexical = Path(path).absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        raise _ambiguous_session("session path escapes loop root")
    current = root
    try:
        value = os.lstat(current)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(value.st_mode):
        raise _ambiguous_session("loop root is not a real directory")
    for component in relative.parts:
        current = current / component
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(value.st_mode):
            raise _ambiguous_session(
                "session path contains a non-directory component"
            )
    return True


def _rollback_created_tree(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the still-identical empty tree created by this transaction."""
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode):
        raise _ambiguous_session("created session tree was replaced before rollback")
    try:
        path.rmdir()
    except OSError as error:
        raise _ambiguous_session("created session tree is nonempty; rollback stopped") from error


def _ambiguous_session(reason: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="ambiguous_session",
        message=f"Host platform session evidence is ambiguous: {reason}",
        recoverable=False,
    )


def _normalize_discovery(discovery: ProjectDiscovery) -> ProjectDiscovery:
    if not isinstance(discovery, ProjectDiscovery):
        raise LoopMemoryError(
            code="invalid_discovery",
            message="Project discovery has an invalid type",
        )
    if discovery.kind not in ("repository", "directory"):
        raise LoopMemoryError(
            code="invalid_discovery",
            message=f"Unsupported project discovery kind: {discovery.kind!r}",
        )
    if not isinstance(discovery.cwd, Path) or not isinstance(discovery.root, Path):
        raise LoopMemoryError(
            code="invalid_discovery",
            message="Project discovery paths must be Path values",
        )
    if discovery.alias is not None and (
        not isinstance(discovery.alias, str) or not discovery.alias.strip()
    ):
        raise LoopMemoryError(
            code="invalid_discovery",
            message="Project alias must be a nonempty string or None",
        )
    return ProjectDiscovery(
        kind=discovery.kind,
        cwd=_normalize_path(discovery.cwd),
        root=_normalize_path(discovery.root),
        alias=(
            str(Path(discovery.alias.strip()).expanduser().resolve(strict=False))
            if discovery.alias is not None
            else None
        ),
    )


def _normalize_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _normalize_legacy_target(loop_root: Path, target: object) -> str:
    if not isinstance(target, str) or not target:
        raise LoopMemoryError(
            code="invalid_legacy_alias",
            message="Legacy alias target must be a nonempty string",
        )
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = loop_root / candidate
    normalized = assert_loop_path(loop_root, candidate)
    return normalized.relative_to(loop_root.resolve(strict=False)).as_posix()


def _normalize_legacy_path(path: Path) -> str:
    if not isinstance(path, Path):
        raise LoopMemoryError(
            code="invalid_legacy_alias",
            message="Legacy alias path must be a Path",
        )
    lexical = Path(os.path.abspath(path.expanduser()))
    if is_reserved_product_path(lexical):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Legacy aliases cannot reference product-owned memory",
            recoverable=False,
        )
    normalized = lexical.parent.resolve(strict=False) / lexical.name
    if is_reserved_product_path(normalized):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Legacy aliases cannot reference product-owned memory",
            recoverable=False,
        )
    return str(normalized)


def _unique_project_match(
    projects: dict[str, object],
    alias_field: str,
    alias: str,
) -> str | None:
    matches: list[str] = []
    for project_id, record in projects.items():
        if not isinstance(record, dict):
            raise _corrupt_state("project record must be an object")
        aliases = record.get(alias_field)
        if not isinstance(aliases, list):
            raise _corrupt_state(f"project {alias_field} must be a list")
        if alias in aliases:
            matches.append(project_id)

    if len(matches) > 1:
        raise LoopMemoryError(
            code="ambiguous_project",
            message="Project alias matches multiple registry records",
            recoverable=False,
        )
    return matches[0] if matches else None


def _add_discovery_aliases(
    record: dict[str, object],
    discovery: ProjectDiscovery,
) -> bool:
    aliases = (("roots", str(discovery.root)),)
    changed = False
    for field, alias in aliases:
        if alias is None:
            continue
        values = record.get(field)
        if not isinstance(values, list):
            raise _corrupt_state(f"project {field} must be a list")
        if alias not in values:
            values.append(alias)
            changed = True
    return changed


def _corrupt_state(detail: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_state",
        message=f"Corrupt registry state: {detail}",
        recoverable=False,
    )


def _bump_generation(state: dict[str, object]) -> None:
    if state.get("schema_version") != 2:
        return
    generation = state.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise _corrupt_state("registry generation is invalid")
    state["generation"] = generation + 1


def _validate_state(state: dict[str, object]) -> None:
    version = state.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _corrupt_state("schema_version must be an integer")
    if version > 2:
        raise LoopMemoryError(
            code="unsupported_schema",
            message=f"Unsupported registry schema version: {version}",
            recoverable=False,
        )
    if version == 2 and frozenset(state) == _ROOT_FIELDS:
        raise LoopMemoryError(
            code="unsupported_schema",
            message="Unsupported registry schema version: 2",
            recoverable=False,
        )
    if version == 2:
        _validate_v2_state(state)
        return
    if version != 1 or frozenset(state) != _ROOT_FIELDS:
        raise _corrupt_state("registry root does not match version 1")

    projects = state["projects"]
    sessions = state["sessions"]
    legacy_aliases = state["legacy_aliases"]
    maintenance = state["maintenance"]
    if not isinstance(projects, dict):
        raise _corrupt_state("projects must be an object")
    if not isinstance(sessions, dict):
        raise _corrupt_state("sessions must be an object")
    if not isinstance(legacy_aliases, dict):
        raise _corrupt_state("legacy_aliases must be an object")
    if not isinstance(maintenance, dict):
        raise _corrupt_state("maintenance must be an object")

    shared_root_owners: set[str] = set()
    root_owners: set[str] = set()
    for project_id, record in projects.items():
        if not _valid_id(project_id, "p"):
            raise _corrupt_state("project ID is invalid")
        if not isinstance(record, dict) or frozenset(record) != _PROJECT_FIELDS:
            raise _corrupt_state("project record shape is invalid")
        kind = record["kind"]
        if kind not in ("repository", "directory"):
            raise _corrupt_state("project kind is invalid")
        for field in ("shared_roots", "roots", "upstream_refs"):
            aliases = record[field]
            if not isinstance(aliases, list) or any(
                not isinstance(alias, str) or not alias for alias in aliases
            ):
                raise _corrupt_state(f"project {field} aliases are invalid")
            if len(aliases) != len(set(aliases)):
                raise _corrupt_state(f"project {field} aliases are duplicated")
        if not record["roots"]:
            raise _corrupt_state("project roots must not be empty")
        for field in ("shared_roots", "roots"):
            if any(_normalized_stored_path(alias) != alias for alias in record[field]):
                raise _corrupt_state(f"project {field} alias is not normalized")
        if any(alias.strip() != alias for alias in record["upstream_refs"]):
            raise _corrupt_state("project upstream_ref alias is not normalized")
        if kind == "repository" and not record["shared_roots"]:
            raise _corrupt_state("Repository project shared_roots must not be empty")
        if kind == "directory" and (record["shared_roots"] or record["upstream_refs"]):
            raise _corrupt_state("directory project has Repository aliases")
        for alias in record["shared_roots"]:
            if alias in shared_root_owners:
                raise _corrupt_state("shared_root alias has multiple project owners")
            shared_root_owners.add(alias)
        for alias in record["roots"]:
            if alias in root_owners:
                raise _corrupt_state("root alias has multiple project owners")
            root_owners.add(alias)

    restorable_sessions: set[tuple[str, str]] = set()
    for session_id, record in sessions.items():
        if not _valid_id(session_id, "s"):
            raise _corrupt_state("session ID is invalid")
        if not isinstance(record, dict) or frozenset(record) != _SESSION_FIELDS:
            raise _corrupt_state("session record shape is invalid")
        project_id = record["project_id"]
        thread_id = record["thread_id"]
        if not _valid_id(project_id, "p") or project_id not in projects:
            raise _corrupt_state("session project reference is invalid")
        if thread_id is not None and (
            not isinstance(thread_id, str) or not thread_id.strip()
        ):
            raise _corrupt_state("session thread_id is invalid")
        if thread_id is not None:
            mapping = (project_id, thread_id)
            if mapping in restorable_sessions:
                raise LoopMemoryError(
                    code="ambiguous_session",
                    message=(
                        "Host thread maps to multiple sessions in project: "
                        f"{project_id}/{thread_id}"
                    ),
                    recoverable=False,
                )
            restorable_sessions.add(mapping)

    for path, record in legacy_aliases.items():
        if not isinstance(path, str) or _normalized_stored_path(path) != path:
            raise _corrupt_state("legacy alias path is not normalized")
        if not isinstance(record, dict) or frozenset(record) != _LEGACY_FIELDS:
            raise _corrupt_state("legacy alias record shape is invalid")
        if any(
            not isinstance(record[field], str) or not record[field]
            for field in _LEGACY_FIELDS
        ):
            raise _corrupt_state("legacy alias record fields are invalid")


def _normalized_stored_path(value: str) -> str | None:
    if not os.path.isabs(value):
        return None
    return os.path.normpath(value)


def _validate_v2_state(state: dict[str, object]) -> None:
    if frozenset(state) != _V2_ROOT_FIELDS:
        raise _corrupt_state("registry root does not match version 2")
    generation = state["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise _corrupt_state("registry generation is invalid")
    projects = state["projects"]
    sessions = state["sessions"]
    aliases = state["legacy_aliases"]
    if not isinstance(projects, dict) or not isinstance(sessions, dict):
        raise _corrupt_state("version 2 projects and sessions must be objects")
    if not isinstance(aliases, dict) or not isinstance(state["maintenance"], dict):
        raise _corrupt_state("version 2 registry collections are invalid")
    root_owners: set[str] = set()
    for project_id, record in projects.items():
        if not _valid_id(project_id, "p") or not isinstance(record, dict):
            raise _corrupt_state("version 2 project record is invalid")
        if frozenset(record) != _V2_PROJECT_FIELDS:
            raise _corrupt_state("version 2 project fields are invalid")
        roots = record["roots"]
        if not isinstance(roots, list) or not roots:
            raise _corrupt_state("version 2 project roots are invalid")
        if len(roots) != len(set(roots)):
            raise _corrupt_state("version 2 project root aliases are duplicated")
        for root in roots:
            if not isinstance(root, str) or not root or not os.path.isabs(root):
                raise _corrupt_state("version 2 project root alias is invalid")
            normalized = os.path.normpath(root)
            if normalized != root:
                raise _corrupt_state("version 2 project root alias is not normalized")
            if root in root_owners:
                raise _corrupt_state("version 2 root alias has multiple owners")
            root_owners.add(root)
    platform_sessions: dict[tuple[str, str], list[tuple[str, dict[str, object]]]] = {}
    for session_id, record in sessions.items():
        if not _valid_id(session_id, "s") or not isinstance(record, dict):
            raise _corrupt_state("version 2 session record is invalid")
        if frozenset(record) != _V2_SESSION_FIELDS:
            raise _corrupt_state("version 2 session fields are invalid")
        project_id = record["project_id"]
        platform = record["platform_session_id"]
        session_generation = record["generation"]
        if not _valid_id(project_id, "p") or project_id not in projects:
            raise _corrupt_state("version 2 session project reference is invalid")
        if platform is not None and (not isinstance(platform, str) or not platform.strip()):
            raise _corrupt_state("version 2 platform session ID is invalid")
        if isinstance(session_generation, bool) or not isinstance(session_generation, int) or session_generation < 1:
            raise _corrupt_state("version 2 session generation is invalid")
        resumes = record["resumes_from"]
        if resumes is not None and (not isinstance(resumes, str) or not _valid_id(resumes, "s")):
            raise _corrupt_state("version 2 resumes_from is invalid")
        state_name = record["state"]
        if state_name not in ("active", "archived"):
            raise _corrupt_state("version 2 session state is invalid")
        if platform is not None:
            platform_sessions.setdefault((project_id, platform), []).append(
                (session_id, record)
            )

    for records in platform_sessions.values():
        ordered = sorted(records, key=lambda pair: int(pair[1]["generation"]))
        generations = [int(record["generation"]) for _, record in ordered]
        if generations != list(range(1, len(generations) + 1)):
            raise _ambiguous_session("session generations are duplicated or discontinuous")
        active = [pair for pair in ordered if pair[1]["state"] == "active"]
        if len(active) > 1 or (active and active[0] != ordered[-1]):
            raise _ambiguous_session("active generation is not uniquely newest")
        for index, (session_id, record) in enumerate(ordered):
            expected_predecessor = None if index == 0 else ordered[index - 1][0]
            if record["resumes_from"] != expected_predecessor:
                raise _ambiguous_session("session generation chain is broken")
            if index < len(ordered) - 1 and record["state"] != "archived":
                raise _ambiguous_session("predecessor generation is not archived")


def _valid_id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(f"{prefix}-")
        and _ID_PATTERN.fullmatch(value) is not None
    )
