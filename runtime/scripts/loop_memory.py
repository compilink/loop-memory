#!/usr/bin/env python3

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


SCRIPTS_DIR = Path(__file__).resolve().parent
PACKAGE_PARENT = SCRIPTS_DIR.parent
for import_root in (SCRIPTS_DIR, PACKAGE_PARENT):
    value = str(import_root)
    if value not in sys.path:
        sys.path.insert(0, value)

from scripts.loopmem import (
    access,
    global_facts,
    legacy,
    maintenance,
    migration,
    root as root_module,
)
from scripts.loopmem.convergence import evaluate_capabilities
from scripts.loopmem.convergence import ConvergenceCacheKey, cached_scope, remember_scope
from scripts.loopmem.errors import (
    AccessDenied,
    EXIT_BLOCKED,
    EXIT_CORRUPT,
    EXIT_OK,
    EXIT_USAGE,
    LoopMemoryError,
)
from scripts.loopmem.paths import (
    default_loop_root,
    discover_project,
    is_reserved_product_path,
    legacy_loop_root,
)
from scripts.loopmem.registry import RegistryStore
from scripts.loopmem.sessions import (
    archive_session,
    ensure_project_layout,
    ensure_session_layout,
    promote_entry,
    session_has_unresolved_outbox,
    write_session_file,
)
from scripts.loopmem.storage import ensure_directory


_AGENT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_MIGRATION_ID = re.compile(r"^m-[0-9a-f]{32}$")


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser(*, json_help: bool = False) -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="loop_memory.py", add_help=not json_help)
    if json_help:
        parser.add_argument("-h", "--help", dest="help_requested", action="store_true")
        parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="operation", required=not json_help)

    enter = _command_parser(commands, "enter", json_help)
    _add_identity_arguments(enter, agent=True)

    preflight = _command_parser(commands, "preflight", json_help)
    _add_identity_arguments(preflight, agent=True)

    access_check = _command_parser(commands, "access-check", json_help)
    _add_root_json_arguments(access_check)

    initialize = _command_parser(commands, "initialize", json_help)
    _add_identity_arguments(initialize, agent=False)

    session_write = _command_parser(commands, "session-write", json_help)
    session_write.add_argument("--cwd", required=True)
    session_write.add_argument("--thread-id", required=True)
    session_write.add_argument(
        "--kind",
        required=True,
        choices=("status", "handoff", "inbox", "outbox"),
    )
    session_write.add_argument("--input", required=True)
    session_write.add_argument("--agent-id")
    _add_root_json_arguments(session_write)

    session_close = _command_parser(commands, "session-close", json_help)
    session_close.add_argument("--cwd", required=True)
    session_close.add_argument("--thread-id", required=True)
    _add_root_json_arguments(session_close)

    promote = _command_parser(commands, "promote", json_help)
    promote.add_argument("--cwd", required=True)
    promote.add_argument("--thread-id", required=True)
    promote.add_argument(
        "--scope",
        required=True,
        choices=("project", "global-long", "global-medium", "global-fact"),
    )
    promote.add_argument("--section", required=True)
    promote.add_argument("--input", required=True)
    _add_root_json_arguments(promote)

    global_organize = _command_parser(commands, "global-organize", json_help)
    global_organize.add_argument("--cwd", required=True)
    global_organize.add_argument("--thread-id", required=True)
    global_organize.add_argument("--methodology", required=True)
    _add_root_json_arguments(global_organize)

    migrate_scan = _command_parser(commands, "migrate-scan", json_help)
    migrate_scan.add_argument("--cwd", required=True)
    migrate_scan.add_argument("--legacy-path", action="append", default=[])
    _add_root_json_arguments(migrate_scan)

    migrate_refresh = _command_parser(commands, "migrate-refresh", json_help)
    migrate_refresh.add_argument("--manifest", required=True)
    _add_root_json_arguments(migrate_refresh)

    legacy_stage = _command_parser(commands, "legacy-stage", json_help)
    legacy_stage.add_argument("--cwd", required=True)
    _add_root_json_arguments(legacy_stage)

    legacy_delete = _command_parser(commands, "legacy-delete", json_help)
    legacy_delete.add_argument("--snapshot", required=True)
    _add_root_json_arguments(legacy_delete)

    migrate_apply = _command_parser(commands, "migrate-apply", json_help)
    migrate_apply.add_argument("--manifest", required=True)
    migrate_apply.add_argument("--classification", required=True)
    migrate_apply.add_argument("--stop-after", choices=("validated",))
    _add_root_json_arguments(migrate_apply)

    maintain = _command_parser(commands, "maintain", json_help)
    maintain.add_argument("--now", type=float)
    _add_root_json_arguments(maintain)

    diagnose = _command_parser(commands, "diagnose", json_help)
    diagnose.add_argument("--cwd", required=True)
    _add_root_json_arguments(diagnose)

    doctor = _command_parser(commands, "doctor", json_help)
    doctor.add_argument("--cwd", required=True)
    _add_root_json_arguments(doctor)
    if json_help:
        _relax_required_arguments(parser)
    return parser


def _command_parser(
    commands: argparse._SubParsersAction,
    name: str,
    json_help: bool,
) -> argparse.ArgumentParser:
    parser = commands.add_parser(name, add_help=not json_help)
    if json_help:
        parser.add_argument(
            "-h",
            "--help",
            dest="help_requested",
            action="store_true",
        )
    return parser


def _relax_required_arguments(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        action.required = False
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                _relax_required_arguments(subparser)


def _add_identity_arguments(
    parser: argparse.ArgumentParser,
    *,
    agent: bool,
) -> None:
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--session-id", "--thread-id", dest="thread_id")
    parser.add_argument("--project-root")
    if agent:
        parser.add_argument("--agent-id")
    _add_root_json_arguments(parser)


def _add_root_json_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root")
    parser.add_argument("--json", action="store_true", required=True)


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _selected_root(value: str | None) -> Path:
    if value is None:
        return default_loop_root()
    return _absolute(value)


def _is_default_root(root: Path) -> bool:
    return root.resolve(strict=False) == default_loop_root()


def _validate_root_components(path: Path) -> None:
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


def _validate_existing_registry(root: Path) -> None:
    path = root / "registry.json"
    try:
        value = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise LoopMemoryError(
            code="corrupt_state",
            message="Loop memory registry is not a regular file",
            recoverable=False,
        )
    if value.st_uid != os.getuid():
        raise LoopMemoryError(
            code="invalid_registry_owner",
            message="Loop memory registry is not owned by the current user",
            recoverable=False,
        )
    store = RegistryStore(root)
    store.validate()


def _prepare_root(root: Path, initialize: bool) -> Path:
    lexical = _absolute(root)
    if lexical == Path(lexical.anchor):
        raise LoopMemoryError(
            code="invalid_loop_root",
            message="Loop root cannot be a filesystem root",
            recoverable=False,
        )
    if is_reserved_product_path(lexical):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory cannot use product-owned memory paths",
            recoverable=False,
        )
    _validate_root_components(lexical)
    try:
        value = lexical.lstat()
    except FileNotFoundError:
        if not initialize:
            raise LoopMemoryError(
                code="invalid_loop_root",
                message="Loop root does not exist",
                recoverable=False,
            )
        ensure_directory(lexical)
        value = lexical.lstat()

    if stat.S_ISLNK(value.st_mode):
        raise LoopMemoryError(
            code="unsafe_path",
            message="Loop root cannot be a symlink",
            recoverable=False,
        )
    if not stat.S_ISDIR(value.st_mode):
        raise LoopMemoryError(
            code="invalid_loop_root",
            message="Loop root must be a real directory",
            recoverable=False,
        )
    if value.st_uid != os.getuid():
        raise LoopMemoryError(
            code="invalid_root_owner",
            message="Loop root is not owned by the current user",
            recoverable=False,
        )
    resolved = lexical.resolve(strict=True)
    if resolved != lexical:
        raise LoopMemoryError(
            code="unsafe_path",
            message="Loop root path is not canonical",
            recoverable=False,
        )
    if is_reserved_product_path(resolved):
        raise LoopMemoryError(
            code="reserved_product_memory",
            message="Loop memory cannot use product-owned memory paths",
            recoverable=False,
        )
    _validate_existing_registry(resolved)
    if initialize:
        RegistryStore(resolved).initialize()
    return resolved


def _resolved_cwd(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_dir():
        raise LoopMemoryError(
            code="invalid_cwd",
            message="Project cwd must be an existing directory",
        )
    return path


def _validate_agent_id(agent_id: str | None) -> None:
    if agent_id is None:
        return
    if (
        agent_id in (".", "..")
        or not _AGENT_ID.fullmatch(agent_id)
    ):
        raise LoopMemoryError(
            code="invalid_agent_id",
            message="Agent ID must be one safe path component",
        )


def _regular_file_bytes(path: Path | str, code: str) -> bytes:
    lexical = _absolute(path)
    try:
        before = lexical.lstat()
    except (FileNotFoundError, OSError) as error:
        raise LoopMemoryError(
            code=code,
            message="Required input file is unavailable",
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise LoopMemoryError(
            code=code,
            message="Required input must be a regular non-symlink file",
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lexical, flags)
    except OSError as error:
        raise LoopMemoryError(
            code=code,
            message="Required input file cannot be opened safely",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise LoopMemoryError(
                code=code,
                message="Required input file changed during validation",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _input_text(path: Path | str) -> str:
    content = _regular_file_bytes(path, "invalid_input_file")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LoopMemoryError(
            code="invalid_input_file",
            message="Required input file must contain valid UTF-8",
        ) from error


def _validated_metadata_file(path: Path | str, code: str) -> Path:
    lexical = _absolute(path)
    _regular_file_bytes(lexical, code)
    return lexical


def _manifest_paths(root: Path) -> list[Path]:
    directory = root / "migrations" / "manifests"
    _validate_root_components(directory)
    try:
        value = directory.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise LoopMemoryError(
            code="corrupt_state",
            message="Migration manifest directory is unsafe",
            recoverable=False,
        )
    paths: list[Path] = []
    for path in sorted(directory.iterdir()):
        match = re.fullmatch(r"(m-[0-9a-f]{32})\.json", path.name)
        if match is None:
            raise LoopMemoryError(
                code="corrupt_state",
                message="Migration manifest directory contains an unexpected entry",
                recoverable=False,
            )
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise LoopMemoryError(
                code="corrupt_state",
                message="Migration manifest is not a regular file",
                recoverable=False,
            )
        paths.append(path)
    return paths


def _validate_migration_namespace(
    root: Path,
    selected_path: Path,
    *,
    recover: bool = True,
) -> dict[str, object]:
    paths = _manifest_paths(root)
    manifests: list[tuple[Path, dict[str, object]]] = []
    manifest_ids: set[str] = set()
    ledger_events = migration.read_ledger_events(root)

    for path in paths:
        manifest = migration.load_manifest(path)
        migration_id = manifest["migration_id"]
        if path.name != f"{migration_id}.json" or migration_id in manifest_ids:
            raise LoopMemoryError(
                code="corrupt_state",
                message="Migration manifest namespace is inconsistent",
                recoverable=False,
            )
        migration.validate_ledger_events(ledger_events, manifest)
        manifest_ids.add(migration_id)
        manifests.append((path, manifest))

    ledger_ids = {event["migration_id"] for event in ledger_events}
    if not ledger_ids.issubset(manifest_ids):
        raise LoopMemoryError(
            code="corrupt_state",
            message="Migration ledger references an unknown manifest",
            recoverable=False,
        )

    selected = _absolute(selected_path)
    if selected not in paths:
        raise LoopMemoryError(
            code="corrupt_state",
            message="Selected migration manifest is outside the validated namespace",
            recoverable=False,
        )

    selected_manifest = next(
        manifest for path, manifest in manifests if path == selected
    )
    if recover:
        for path, manifest in manifests:
            if path != selected and manifest["state"] != "complete":
                migration.recover_migration(root, path)
        migration.recover_migration(root, selected)
    return selected_manifest


def _check_pending_migrations(
    root: Path,
    project_id: str | None = None,
) -> None:
    pending: list[str] = []
    ledger_events = migration.read_ledger_events(root)
    for path in _manifest_paths(root):
        manifest = migration.load_manifest(path)
        if project_id is not None and (
            manifest["source_kind"] != "global"
            and manifest["project_id"] != project_id
        ):
            continue
        migration.validate_ledger_events(ledger_events, manifest)
        state = manifest["state"]
        source = Path(manifest["source"])
        if (
            manifest["source_kind"] != "global"
            and
            migration.is_refresh_metadata_eligible(manifest)
            and legacy.has_staged_receipt(
                root,
                source,
                migration.inventory_sha256(manifest["files"]),
            )
        ):
            continue
        if state in ("quarantined", "complete"):
            manifest = migration.recover_migration(root, path)
            state = manifest["state"]
        else:
            try:
                migration.recover_migration(root, path)
            except LoopMemoryError as error:
                if (
                    error.code == "source_changed"
                    and migration.is_refresh_metadata_eligible(manifest)
                    and migration.is_refresh_source_compatible(manifest)
                ):
                    raise LoopMemoryError(
                        code="migration_refresh_required",
                        message=(
                            "Migration inventory changed; run migrate-refresh "
                            f"--manifest {path}"
                        ),
                        recoverable=True,
                    ) from error
                raise
        if state != "complete":
            pending.append(manifest["migration_id"])
    if pending:
        raise LoopMemoryError(
            code="migration_required",
            message="A validated legacy migration requires explicit completion",
        )


def _implicit_legacy_candidates(root: Path, cwd: Path) -> list[Path]:
    discovery = discover_project(cwd)
    current = (
        discovery.root / ".memory"
        if discovery.kind == "repository"
        else cwd / ".memory"
    )
    candidates = [current]
    if _is_default_root(root):
        candidates.append((Path.home() / ".codex" / ".memory").resolve(strict=False))
    return candidates


def _detect_legacy(root: Path, cwd: Path, project_id: str) -> None:
    store = RegistryStore(root)
    candidates = _implicit_legacy_candidates(root, cwd)
    project_candidate = candidates[0]
    project_legacy = migration.inspect_project_legacy_source(cwd)
    if project_legacy["exists"] is True:
        inventoried_sources = {
            Path(migration.load_manifest(path)["source"])
            for path in _manifest_paths(root)
        }
        if (
            store.resolve_legacy_alias(project_candidate) is None
            and project_candidate not in inventoried_sources
        ):
            raise LoopMemoryError(
                code="legacy_memory_required",
                message=(
                    "Project legacy memory requires Agent-guided staging and "
                    "semantic migration"
                ),
            )
    unmigrated: list[Path] = []
    for candidate in candidates:
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            value = None
        except OSError as error:
            raise LoopMemoryError(
                code="unsafe_legacy_source",
                message="Legacy memory candidate could not be inspected safely",
                recoverable=False,
            ) from error
        if value is not None and (
            stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode)
        ):
            raise LoopMemoryError(
                code="unsafe_legacy_source",
                message="Legacy memory candidate must be a real directory",
                recoverable=False,
            )

        alias = store.resolve_legacy_alias(candidate)
        if alias is not None:
            migration_id = alias["migration_id"]
            if not _MIGRATION_ID.fullmatch(migration_id):
                raise LoopMemoryError(
                    code="corrupt_state",
                    message="Legacy alias has an invalid migration identity",
                    recoverable=False,
                )
            manifest_path = root / "migrations" / "manifests" / f"{migration_id}.json"
            manifest = migration.recover_migration(root, manifest_path)
            if manifest["state"] != "complete" or manifest["target"] != alias["target"]:
                raise LoopMemoryError(
                    code="corrupt_state",
                    message="Legacy alias does not match a completed migration",
                    recoverable=False,
                )
            continue
        if value is None:
            continue
        unmigrated.append(candidate)

    if unmigrated:
        implicit = candidates[0]
        extras = [candidate for candidate in unmigrated if candidate != implicit]
        migration.scan_legacy(root, cwd, extras)
    _check_pending_migrations(root, project_id)


def _has_unrelated_project_migration(root: Path, project_id: str) -> bool:
    for path in _manifest_paths(root):
        manifest = migration.load_manifest(path)
        if (
            manifest["state"] != "complete"
            and manifest["source_kind"] != "global"
            and manifest["project_id"] != project_id
        ):
            return True
    return False


def _guard_legacy_stage(root: Path, cwd: Path) -> None:
    inspection = migration.inspect_project_legacy_source(cwd)
    if inspection["exists"] is not True:
        return
    source = Path(inspection["source"])
    matches: list[Path] = []
    for path in _manifest_paths(root):
        if Path(migration.load_manifest(path)["source"]) == source:
            matches.append(path)
    if len(matches) > 1:
        raise LoopMemoryError(
            code="corrupt_state",
            message="Multiple migration manifests claim the project legacy source",
            recoverable=False,
        )
    if not matches:
        return

    manifest = _validate_migration_namespace(root, matches[0], recover=False)
    if not migration.is_refresh_metadata_eligible(manifest):
        raise LoopMemoryError(
            code="migration_required",
            message="A later migration state must be resolved before legacy staging",
        )
    if (
        migration.inventory_sha256(manifest["files"])
        == inspection["inventory_sha256"]
    ):
        return
    if migration.is_refresh_source_compatible(manifest):
        raise LoopMemoryError(
            code="migration_refresh_required",
            message=f"Migration inventory changed; run migrate-refresh --manifest {matches[0]}",
        )
    raise LoopMemoryError(
        code="source_changed",
        message="Legacy source changed incompatibly after migration inventory",
        recoverable=False,
    )


def _identity_paths(
    root: Path,
    project_id: str,
    session_id: str,
    agent_id: str | None,
) -> dict[str, str]:
    project = root / "projects" / project_id
    session = project / "sessions" / "active" / session_id
    if agent_id is None:
        agent = session / "agents" / "main"
    else:
        agent = session / "agents" / "subagents" / agent_id
    return {
        "global": str(root / "global"),
        "global_long": str(root / "global" / "long.md"),
        "global_medium": str(root / "global" / "medium.md"),
        "global_short": str(root / "global" / "short.md"),
        "global_fact_index": str(root / "global" / "facts" / "index.md"),
        "global_facts": str(root / "global" / "facts" / "entries"),
        "project": str(project),
        "project_memory": str(project / "project.md"),
        "session": str(session),
        "status": str(session / "status.md"),
        "handoff": str(session / "handoff.md"),
        "agent_inbox": str(agent / "inbox.md"),
        "agent_outbox": str(agent / "outbox.md"),
    }


def _current_scope_digest(root: Path, project_id: str, session_id: str) -> str:
    project = root / "projects" / project_id
    session = project / "sessions" / "active" / session_id
    files = [project / "project.md", session / "status.md", session / "handoff.md",
             session / "agents" / "main" / "inbox.md", session / "agents" / "main" / "outbox.md"]
    digest = hashlib.sha256()
    for path in files:
        try:
            value = path.stat()
            digest.update(str((path.name, value.st_ino, value.st_size, value.st_mtime_ns)).encode())
        except FileNotFoundError:
            digest.update(str((path.name, None)).encode())
    return digest.hexdigest()


def _scan_current_scope(root: Path, project_id: str, session_id: str) -> dict[str, object]:
    session = root / "projects" / project_id / "sessions" / "active" / session_id
    return {"unresolved_outbox": session_has_unresolved_outbox(session)}


def _materialize_session_with_rollback(
    root: Path,
    project_id: str,
    session_id: str,
):
    """Create additive session layout and return identity-safe rollback."""
    session = ensure_session_layout(root, project_id, session_id)
    identity = (session.stat().st_dev, session.stat().st_ino)

    def rollback() -> None:
        current = session.lstat()
        if (current.st_dev, current.st_ino) != identity or session.is_symlink():
            raise LoopMemoryError(
                code="ambiguous_session",
                message="Created session tree changed before rollback",
                recoverable=False,
            )
        expected = {
            Path("status.md"), Path("handoff.md"),
            Path("agents/main/inbox.md"), Path("agents/main/outbox.md"),
        }
        files = {
            path.relative_to(session)
            for path in session.rglob("*") if path.is_file() and not path.is_symlink()
        }
        if files != expected:
            raise LoopMemoryError(
                code="ambiguous_session",
                message="Created session tree contains foreign content; rollback stopped",
                recoverable=False,
            )
        for path in sorted(session.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink():
                raise LoopMemoryError(
                    code="ambiguous_session",
                    message="Created session tree contains a symlink; rollback stopped",
                    recoverable=False,
                )
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        session.rmdir()

    return rollback


def _identity_preflight(
    root: Path,
    cwd_value: str,
    thread_id: str | None,
    agent_id: str | None,
    project_root_value: str | None = None,
) -> dict[str, object]:
    cwd = _resolved_cwd(cwd_value)
    _validate_agent_id(agent_id)
    project_root = (
        _resolved_cwd(project_root_value)
        if project_root_value is not None
        else None
    )
    # First authority operation. Probe a missing destination through its parent
    # so the permission check cannot publish a second root before relocation.
    access.check_access(root, materialize_missing=False)
    selected_default = root.resolve(strict=False) == default_loop_root()
    if selected_default:
        old_root = legacy_loop_root()
        if old_root.exists() and root.exists():
            # Relocation owns the uniqueness decision; do not let ordinary
            # new-root recovery mutate either authority first.
            root = root_module.relocate_root(old_root, root)
        elif old_root.exists() or not root.exists() or (root / "relocation.json").exists():
            root = root_module.relocate_root(old_root, root)
    # Validate the current external legacy boundary before any Loop metadata
    # conversion can publish writes.  The returned snapshot contains only
    # inventory/risk metadata, never memory bodies.
    legacy_inspection = migration.inspect_project_legacy_source(
        project_root if project_root is not None else cwd
    )
    prepared = _prepare_root(root, initialize=True)
    plan = root_module.convert_v1_metadata(prepared)
    root_module.validate_conversion_plan(prepared, plan)
    root_module.publish_conversion(prepared, plan)
    RegistryStore(prepared).initialize_v2()
    discovery = discover_project(cwd, project_root)
    store = RegistryStore(prepared)
    project_id = store.resolve_project(discovery, create=True)
    if project_id is None:
        raise LoopMemoryError(
            code="corrupt_state",
            message="Registry did not create a project identity",
            recoverable=False,
        )
    # Namespace corruption is a global fail-closed condition.  Reject it
    # before publishing a session record or materializing any memory body
    # layout; valid pending migrations are evaluated later by scope.
    _manifest_paths(prepared)
    session_info = store.resolve_session_info(
        project_id,
        thread_id,
        create=True,
        materialize_active=lambda created_session_id: _materialize_session_with_rollback(
            prepared, project_id, created_session_id
        ),
    )
    if session_info is None:
        raise LoopMemoryError(
            code="corrupt_state",
            message="Registry did not create a session identity",
            recoverable=False,
        )
    session_id = str(session_info["session_id"])
    # Existing generations still converge additive templates here; new
    # generations are materialized by RegistryStore while its mutation lease is
    # held, before their registry record is published.
    ensure_project_layout(prepared, project_id)
    ensure_session_layout(prepared, project_id, session_id)
    global_long_organization_due = False
    try:
        global_facts.validate_long_document(
            (prepared / "global/long.md").read_text(encoding="utf-8")
        )
        global_facts.validate_fact_index(prepared)
    except LoopMemoryError as error:
        if error.code == "global_long_not_canonical":
            global_long_organization_due = True
        else:
            raise
    protected = False
    credential = legacy_inspection.get("has_credential_assignment") is True
    ambiguous = False
    migration_conflict = False
    session_recovered = bool(session_info.get("session_recovered"))
    unrelated = _has_unrelated_project_migration(prepared, project_id)
    try:
        _detect_legacy(prepared, cwd, project_id)
    except LoopMemoryError as error:
        if error.code in {
            "legacy_memory_required", "migration_required",
            "legacy_stage_required", "migration_refresh_required",
            "source_changed", "migration_conflict",
        }:
            if error.code == "migration_required":
                # Global authority problems are not local degradation.  A
                # current-project migration is safe to represent by scope.
                current_manifest = False
                for path in _manifest_paths(prepared):
                    manifest = migration.load_manifest(path)
                    if (
                        manifest["source_kind"] != "global"
                        and manifest["project_id"] == project_id
                    ):
                        current_manifest = True
                        break
                if not current_manifest:
                    raise
                ambiguous = True
            elif error.code == "migration_conflict":
                migration_conflict = True
            else:
                protected = False
                ambiguous = error.code in {
                    "legacy_memory_required", "legacy_stage_required",
                    "migration_refresh_required", "source_changed"
                }
        else:
            raise
    root_meta = json.loads((prepared / "root.json").read_text(encoding="utf-8"))
    registry_meta = json.loads((prepared / "registry.json").read_text(encoding="utf-8"))
    scope_digest = _current_scope_digest(prepared, project_id, session_id)
    cache_key = ConvergenceCacheKey(
        str(root_meta["root_id"]), int(root_meta["generation"]),
        int(registry_meta.get("generation", 1)), str(cwd),
        str(project_root or cwd), scope_digest,
    )
    cached = cached_scope(cache_key)
    if cached is None:
        scope_probe = _scan_current_scope(prepared, project_id, session_id)
        remember_scope(cache_key, scope_probe)
    else:
        scope_probe = cached
    # Outboxes are live coordination state; never trust a cache for close.
    unresolved_outbox = session_has_unresolved_outbox(
        prepared / "projects" / project_id / "sessions" / "active" / session_id
    )
    capabilities, notices = evaluate_capabilities(
        protected_current_project_legacy=protected,
        credential_current_project_legacy=credential,
        ambiguous_project_facts=ambiguous,
        migration_conflict=migration_conflict,
        unrelated_project_migration=unrelated,
        unresolved_outbox=unresolved_outbox,
        global_long_organization_due=global_long_organization_due,
        session_recovered=session_recovered,
    )
    identity = {
        "root": prepared,
        "project_id": project_id,
        "session_id": session_id,
        "session_generation": session_info["session_generation"],
        "resumes_from": session_info["resumes_from"],
        "resume_handoff": session_info["resume_handoff"],
        "agent_id": agent_id,
        "paths": _identity_paths(
            prepared,
            project_id,
            session_id,
            agent_id,
        ),
        "warnings": [],
        "degraded": bool(notices and any(notice.blocking for notice in notices)),
        "capabilities": capabilities.as_dict(),
        "notices": [notice.as_dict() for notice in notices],
    }
    if session_recovered:
        identity["session_recovered"] = True
    return identity


def _success(
    operation: str,
    root: Path,
    warnings: list[object] | None = None,
    **fields: object,
) -> dict[str, object]:
    return {
        "ok": True,
        "operation": operation,
        "root": str(root),
        "warnings": list(warnings or []),
        **fields,
    }


def _require_capability(identity: dict[str, object], name: str) -> None:
    capabilities = identity.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get(name) is not True:
        raise LoopMemoryError(
            code="capability_denied",
            message=f"Loop memory capability is unavailable: {name}",
        )


def _archived_session_result(
    root: Path,
    cwd_value: str,
    thread_id: str | None,
) -> dict[str, object] | None:
    """Preserve close idempotence without entering/resuming an archive."""
    prepared = _prepare_root(root, initialize=False)
    store = RegistryStore(prepared)
    project_id = store.resolve_project(
        discover_project(_resolved_cwd(cwd_value)),
        create=False,
    )
    if project_id is None:
        return None
    session_id = store.resolve_session(project_id, thread_id, create=False)
    if session_id is None:
        return None
    archive_root = prepared / "projects" / project_id / "sessions" / "archive"
    matches = sorted(archive_root.glob(f"????-??/{session_id}"))
    if len(matches) == 1 and matches[0].is_dir() and not matches[0].is_symlink():
        return {
            "root": prepared,
            "project_id": project_id,
            "session_id": session_id,
            "path": matches[0],
        }
    if len(matches) > 1:
        raise LoopMemoryError(
            code="ambiguous_session",
            message="Session has multiple archive locations",
            recoverable=False,
        )
    return None


def _dispatch(arguments: argparse.Namespace) -> dict[str, object]:
    operation = arguments.operation
    selected = _selected_root(arguments.root)

    if operation in ("enter", "preflight", "initialize"):
        identity = _identity_preflight(
            selected,
            arguments.cwd,
            arguments.thread_id,
            getattr(arguments, "agent_id", None),
            arguments.project_root,
        )
        root = identity.pop("root")
        warnings = identity.pop("warnings")
        return _success(operation, root, warnings, **identity)

    if operation == "access-check":
        access.check_access(selected)
        root = _absolute(selected)
        return _success(operation, root)

    if operation == "session-write":
        identity = _identity_preflight(
            selected,
            arguments.cwd,
            arguments.thread_id,
            arguments.agent_id,
        )
        _require_capability(identity, "session_write")
        value = _input_text(arguments.input)
        path = write_session_file(
            identity["root"],
            identity["project_id"],
            identity["session_id"],
            arguments.kind,
            value,
            arguments.agent_id,
        )
        return _success(
            operation,
            identity["root"],
            identity["warnings"],
            project_id=identity["project_id"],
            session_id=identity["session_id"],
            agent_id=identity["agent_id"],
            paths=identity["paths"],
            path=str(path),
        )

    if operation == "session-close":
        # A repeated close of an already archived generation is idempotent;
        # an explicit later enter is what requests a successor generation.
        archived_before_enter = _archived_session_result(
            selected,
            arguments.cwd,
            arguments.thread_id,
        )
        if archived_before_enter is not None:
            return _success(
                operation,
                archived_before_enter["root"],
                [],
                project_id=archived_before_enter["project_id"],
                session_id=archived_before_enter["session_id"],
                path=str(archived_before_enter["path"]),
            )
        # Every mutating close path enters first; the idempotent archive lookup
        # is only a post-enter read of already-authorized state.
        try:
            identity = _identity_preflight(
                selected,
                arguments.cwd,
                arguments.thread_id,
                None,
            )
            _require_capability(identity, "session_close")
        except LoopMemoryError as error:
            if error.code != "session_archived":
                raise
            # enter performed access, authority validation, identity resolution,
            # and archive detection; continue with the idempotent read branch.
            identity = {
                "root": _prepare_root(selected, initialize=False),
                "project_id": None,
                "session_id": arguments.thread_id,
                "capabilities": {"session_close": True},
            }
        archived = _archived_session_result(
            identity["root"],
            arguments.cwd,
            arguments.thread_id,
        )
        if archived is not None:
            return _success(
                operation,
                archived["root"],
                [],
                project_id=archived["project_id"],
                session_id=archived["session_id"],
                path=str(archived["path"]),
            )
        root = identity["root"]
        project_id = identity["project_id"]
        session_id = identity["session_id"]
        path = archive_session(
            root,
            project_id,
            session_id,
            require_resolved_outboxes=True,
        )
        RegistryStore(root).mark_session_archived(project_id, session_id)
        return _success(
            operation,
            root,
            [],
            project_id=project_id,
            session_id=session_id,
            path=str(path),
        )

    if operation == "promote":
        identity = _identity_preflight(
            selected,
            arguments.cwd,
            arguments.thread_id,
            None,
        )
        required = (
            "project_promote"
            if arguments.scope == "project"
            else "global_promote"
        )
        _require_capability(identity, required)
        entry = _input_text(arguments.input)
        changed = promote_entry(
            identity["root"],
            identity["project_id"],
            arguments.scope,
            arguments.section,
            entry,
        )
        return _success(
            operation,
            identity["root"],
            identity["warnings"],
            project_id=identity["project_id"],
            session_id=identity["session_id"],
            paths=identity["paths"],
            changed=changed,
        )

    if operation == "global-organize":
        identity = _identity_preflight(
            selected,
            arguments.cwd,
            arguments.thread_id,
            None,
        )
        _require_capability(identity, "global_promote")
        methodology = _input_text(arguments.methodology)
        result = global_facts.organize_global_long(identity["root"], methodology)
        return _success(
            operation,
            identity["root"],
            identity["warnings"],
            project_id=identity["project_id"],
            session_id=identity["session_id"],
            paths=identity["paths"],
            **result,
        )

    if operation == "migrate-scan":
        root = _prepare_root(selected, initialize=True)
        _manifest_paths(root)
        cwd = _resolved_cwd(arguments.cwd)
        candidates = [Path(value) for value in arguments.legacy_path]
        if _is_default_root(root):
            global_legacy = (Path.home() / ".codex" / ".memory").resolve(
                strict=False
            )
            if global_legacy.exists() and global_legacy not in candidates:
                candidates.append(global_legacy)
        result = migration.scan_legacy(root, cwd, candidates)
        return _success(
            operation,
            root,
            result["warnings"],
            manifests=result["manifests"],
            excluded=result["excluded"],
        )

    if operation == "migrate-refresh":
        root = _prepare_root(selected, initialize=True)
        manifest_path = _validated_metadata_file(
            arguments.manifest,
            "invalid_manifest_file",
        )
        result = migration.refresh_migration(
            root,
            manifest_path,
            namespace_validator=lambda: _validate_migration_namespace(
                root,
                manifest_path,
                recover=False,
            ),
        )
        return _success(
            operation,
            root,
            result.get("warnings", []),
            migration=result,
        )

    if operation == "legacy-stage":
        root = _prepare_root(selected, initialize=True)
        cwd = _resolved_cwd(arguments.cwd)
        _guard_legacy_stage(root, cwd)
        result = legacy.stage_legacy(root, cwd)
        return _success(operation, root, [], **result)

    if operation == "legacy-delete":
        root = _prepare_root(selected, initialize=False)
        result = legacy.delete_legacy(root, arguments.snapshot)
        return _success(operation, root, [], **result)

    if operation == "migrate-apply":
        root = _prepare_root(selected, initialize=True)
        manifest_path = _validated_metadata_file(
            arguments.manifest,
            "invalid_manifest_file",
        )
        classification_path = _absolute(arguments.classification)
        selected_manifest = migration.load_manifest(manifest_path)
        classification_snapshot = migration.load_classification_snapshot(
            classification_path,
            selected_manifest,
            root,
        )
        migration.restore_missing_v2_snapshot(root, manifest_path)
        selected_manifest = _validate_migration_namespace(
            root,
            manifest_path,
            recover=False,
        )
        result = migration.apply_migration(
            root,
            manifest_path,
            classification_path,
            arguments.stop_after,
            classification_snapshot=classification_snapshot,
        )
        return _success(
            operation,
            root,
            result.get("warnings", []),
            migration=result,
        )

    if operation == "maintain":
        root = _prepare_root(selected, initialize=False)
        now = time.time() if arguments.now is None else arguments.now
        result = maintenance.maintain(root, now)
        return _success(
            operation,
            root,
            result["warnings"],
            deleted=result["deleted"],
            preserved=result["preserved"],
        )

    if operation in ("diagnose", "doctor"):
        root = _selected_root(arguments.root)
        cwd = _resolved_cwd(arguments.cwd)
        result = (
            maintenance.doctor(root, cwd)
            if operation == "doctor"
            else maintenance.diagnose(root, cwd)
        )
        root_metadata = result.pop("root")
        result.pop("operation")
        return _success(
            operation,
            root,
            [],
            root_metadata=root_metadata,
            **result,
        )

    raise LoopMemoryError(
        code="usage",
        message="Unsupported operation",
    )


def _write_json(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")


def _failure(error: LoopMemoryError) -> dict[str, object]:
    payload: dict[str, object] = {"ok": False, "error": error.as_dict()}
    if isinstance(error, AccessDenied):
        payload["required_access"] = error.required_access.as_dict()
        payload["next_action"] = error.next_action
    return payload


def _exit_for_error(error: LoopMemoryError) -> int:
    return EXIT_BLOCKED if error.recoverable else EXIT_CORRUPT


def _json_help_payload(raw_arguments: list[str]) -> dict[str, object] | None:
    if "--json" not in raw_arguments or not any(
        value in ("-h", "--help") for value in raw_arguments
    ):
        return None

    arguments = _parser(json_help=True).parse_args(raw_arguments)
    if not getattr(arguments, "help_requested", False):
        raise _UsageError("JSON help requires --help")
    return _success(
        "help",
        _selected_root(getattr(arguments, "root", None)),
        command=arguments.operation,
    )


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_arguments
    try:
        payload = _json_help_payload(raw_arguments)
        if payload is None:
            arguments = _parser().parse_args(raw_arguments)
            payload = _dispatch(arguments)
    except _UsageError:
        error = LoopMemoryError(
            code="usage",
            message="Invalid command usage",
        )
        if json_requested:
            _write_json(_failure(error))
        else:
            sys.stderr.write("usage: invalid command usage\n")
        return EXIT_USAGE
    except LoopMemoryError as error:
        if json_requested:
            _write_json(_failure(error))
        else:
            sys.stderr.write(f"{error.code}: {error.message}\n")
        return _exit_for_error(error)
    except OSError as caught:
        if isinstance(caught, PermissionError) or caught.errno in (
            errno.EACCES,
            errno.EPERM,
        ):
            error = AccessDenied()
        else:
            error = LoopMemoryError(
                code="internal_error",
                message="Loop memory state could not be processed safely",
                recoverable=False,
            )
        if json_requested:
            _write_json(_failure(error))
        else:
            sys.stderr.write(f"{error.code}: {error.message}\n")
        return _exit_for_error(error)
    except Exception:
        error = LoopMemoryError(
            code="internal_error",
            message="Loop memory state could not be processed safely",
            recoverable=False,
        )
        if json_requested:
            _write_json(_failure(error))
        else:
            sys.stderr.write(f"{error.code}: {error.message}\n")
        return EXIT_CORRUPT

    _write_json(payload)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
