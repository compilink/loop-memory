import ctypes
from datetime import datetime
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
import sys

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import global_facts
from scripts.loopmem.paths import assert_loop_path
from scripts.loopmem.storage import FileLease, ensure_directory, write_text_atomic


PROJECT_SECTIONS = (
    "Verified Facts",
    "Engineering Patterns",
    "Decisions",
    "Risks",
    "Superseded",
)

_GLOBAL_TEMPLATES = {
    "long.md": global_facts.LONG_TEMPLATE,
    "medium.md": "# Global Medium-Term Memory\n\n## Entries\n",
    "short.md": "# Global Short-Term Memory\n\n## Entries\n",
}
_PROJECT_TEMPLATE = (
    "# Project Memory\n"
    "\n"
    "## Verified Facts\n"
    "\n"
    "## Engineering Patterns\n"
    "\n"
    "## Decisions\n"
    "\n"
    "## Risks\n"
    "\n"
    "## Superseded\n"
)
_PROJECT_HORIZON_TEMPLATES = {
    "long": "# Project Long-Term Memory\n\n## Entries\n",
    "medium": "# Project Medium-Term Memory\n\n## Entries\n",
    "short": "# Project Short-Term Memory\n\n## Entries\n",
}
_SESSION_TEMPLATES = {
    "status.md": "# Session Status\n",
    "handoff.md": "# Session Handoff\n",
}
_MAIN_AGENT_TEMPLATES = {
    "inbox.md": "# Main Agent Inbox\n",
    "outbox.md": "# Main Agent Outbox\n",
}
_ID_PATTERN = re.compile(r"^[ps]-[A-Za-z0-9][A-Za-z0-9._-]*$")
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_ENTRY_FIRST_LINE = re.compile(
    r"^- \[(\d{4}-\d{2}-\d{2})\]"
    r"\[(verified|inferred|superseded)\] (\S.*)$"
)
_TRAILING_HORIZONTAL_SPACE = re.compile(r"[ \t]+$")
_VALID_SCOPES = frozenset(
    (
        "project",
        "project-long",
        "project-medium",
        "project-short",
        "global-long",
        "global-medium",
        "global-fact",
    )
)
_VALID_KINDS = frozenset(("status", "handoff", "inbox", "outbox"))
_ARCHIVE_MONTH_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
MAX_SESSION_BYTES = 16 * 1024


def ensure_global_layout(loop_root: Path) -> None:
    root = _canonical_root(loop_root)
    _ensure_global_layout(root)


def ensure_project_layout(loop_root: Path, project_id: str) -> Path:
    root = _canonical_root(loop_root)
    _validate_id(project_id, "p", "project")
    return _ensure_project_layout(root, project_id)


def ensure_session_layout(
    loop_root: Path,
    project_id: str,
    session_id: str,
    *,
    materialize_files: bool = True,
) -> Path:
    root = _canonical_root(loop_root)
    _validate_id(project_id, "p", "project")
    _validate_id(session_id, "s", "session")
    project_dir = _ensure_project_layout(root, project_id)
    sessions_dir = _target(root, project_dir, "sessions")
    with _SessionLifecycleFlock(sessions_dir, fcntl.LOCK_SH):
        _assert_session_not_archived(root, sessions_dir, session_id)
        return _ensure_active_session_layout(
            root, project_dir, session_id, materialize_files=materialize_files
        )


def write_session_file(
    loop_root: Path,
    project_id: str,
    session_id: str,
    kind: str,
    value: str,
    agent_id: str | None = None,
) -> Path:
    root = _canonical_root(loop_root)
    _validate_id(project_id, "p", "project")
    _validate_id(session_id, "s", "session")
    if kind not in _VALID_KINDS:
        raise LoopMemoryError(
            code="invalid_session_file_kind",
            message="Session file kind must be status, handoff, inbox, or outbox",
        )
    if agent_id is not None:
        _validate_agent_id(agent_id)
    if kind in ("status", "handoff") and agent_id is not None:
        raise LoopMemoryError(
            code="invalid_agent_scope",
            message=f"Subagents cannot write the shared session {kind} file",
        )
    if not isinstance(value, str):
        raise LoopMemoryError(
            code="invalid_session_file_value",
            message="Session file value must be text",
        )
    if not value.strip():
        raise LoopMemoryError(
            code="empty_memory_write",
            message="Session memory writes must contain non-whitespace content",
        )
    if len(value.encode("utf-8")) > MAX_SESSION_BYTES:
        raise LoopMemoryError(
            code="memory_write_too_large",
            message=f"Session memory writes must be at most {MAX_SESSION_BYTES} bytes",
        )
    template = _session_template(kind, agent_id)
    template_only = value.strip() == template.strip()
    project_dir = _ensure_project_layout(root, project_id)
    sessions_dir = _target(root, project_dir, "sessions")
    with _SessionLifecycleFlock(sessions_dir, fcntl.LOCK_SH):
        _assert_session_not_archived(root, sessions_dir, session_id)
        session_dir = _ensure_active_session_layout(
            root, project_dir, session_id, materialize_files=False
        )
        if kind in ("status", "handoff"):
            destination = _target(root, session_dir, f"{kind}.md")
        elif agent_id is None:
            destination = _target(root, session_dir, "agents", "main", f"{kind}.md")
        else:
            agent_dir = _target(
                root,
                session_dir,
                "agents",
                "subagents",
                agent_id,
            )
            ensure_directory(agent_dir)
            destination = _target(root, agent_dir, f"{kind}.md")

        if template_only and not destination.exists():
            raise LoopMemoryError(
                code="template_only_write",
                message="Session memory writes must contain content beyond the template",
            )

        encoded = value.encode("utf-8")
        try:
            existing = destination.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise LoopMemoryError(
                    code="unsafe_path",
                    message="Session memory destination must be a regular file",
                    recoverable=False,
                )
            if destination.read_bytes() == encoded:
                return destination
        write_text_atomic(destination, value)
        return destination


def promote_entry(
    loop_root: Path,
    project_id: str,
    scope: str,
    section: str,
    entry: str,
) -> bool:
    root = _canonical_root(loop_root)
    _validate_id(project_id, "p", "project")
    if scope not in _VALID_SCOPES:
        raise LoopMemoryError(
            code="invalid_scope",
            message=(
                "Promotion scope must be project, project-long, project-medium, "
                "project-short, global-long, global-medium, or global-fact"
            ),
        )
    _validate_promotion_section(scope, section)
    normalized_entry, status = _normalize_entry(entry)
    _validate_promotion_status(scope, section, status)

    if scope == "global-fact":
        return bool(global_facts.promote_fact(loop_root, entry)["changed"])

    if scope == "project":
        destination = _target(root, "projects", project_id, "project.md")
        lease_name = f"promote-project-{project_id}.lock"
    elif scope.startswith("project-"):
        horizon = scope.removeprefix("project-")
        destination = _target(root, "projects", project_id, f"{horizon}.md")
        lease_name = f"promote-project-{horizon}-{project_id}.lock"
    else:
        horizon = "long" if scope == "global-long" else "medium"
        destination = _target(root, "global", f"{horizon}.md")
        lease_name = f"promote-global-{horizon}.lock"

    lease_path = _target(root, "locks", lease_name)
    with FileLease(lease_path, owner=f"promote:{scope}"):
        if scope == "project":
            project_dir = _ensure_project_layout(root, project_id)
            destination = _target(root, project_dir, "project.md")
        elif scope.startswith("project-"):
            project_dir = _ensure_project_layout(root, project_id)
            horizon = scope.removeprefix("project-")
            destination = _target(root, project_dir, f"{horizon}.md")
            if not destination.exists():
                _ensure_file(destination, _PROJECT_HORIZON_TEMPLATES[horizon])
        else:
            _ensure_global_layout(root)
            destination = _target(root, "global", f"{horizon}.md")
        original = _read_destination(destination)
        section_start, section_end = _find_section(original, section, destination)
        section_content = original[section_start:section_end]
        if _contains_normalized_entry(section_content, normalized_entry, destination):
            return False

        entry_bytes = normalized_entry.encode("utf-8")
        prefix = _entry_prefix(original[:section_end])
        suffix = b"\n" if section_end < len(original) else b""
        updated = (
            original[:section_end]
            + prefix
            + entry_bytes
            + suffix
            + original[section_end:]
        )
        try:
            updated_text = updated.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _corrupt_memory_file(destination, "file is not UTF-8") from error
        write_text_atomic(destination, updated_text)
        return True


def archive_session(
    loop_root: Path,
    project_id: str,
    session_id: str,
    now: datetime | None = None,
    *,
    require_resolved_outboxes: bool = False,
) -> Path:
    root = _canonical_root(loop_root)
    _validate_id(project_id, "p", "project")
    _validate_id(session_id, "s", "session")
    if now is None:
        now = datetime.now()
    elif not isinstance(now, datetime):
        raise LoopMemoryError(
            code="invalid_archive_time",
            message="Archive time must be a datetime or None",
        )

    project_dir = _target(root, "projects", project_id)
    sessions_dir = _target(root, project_dir, "sessions")
    active_parent = _target(root, sessions_dir, "active")
    source = _target(root, active_parent, session_id)
    archive_root = _target(root, sessions_dir, "archive")
    archive_parent = _target(root, archive_root, now.strftime("%Y-%m"))
    destination = _target(root, archive_parent, session_id)
    lease_path = _target(root, "locks", f"archive-{project_id}-{session_id}.lock")

    with FileLease(lease_path, owner=f"archive:{project_id}:{session_id}"):
        if not sessions_dir.is_dir():
            raise _session_not_found(project_id, session_id)
        with _SessionLifecycleFlock(sessions_dir, fcntl.LOCK_EX):
            source_exists = _real_directory_exists(
                source,
                session_id,
                "active session",
            )
            archived = _find_archived_session(root, sessions_dir, session_id)
            if source_exists and archived is not None:
                raise _corrupt_archives(
                    session_id,
                    f"active and archived copies coexist: {source}, {archived}",
                )
            if not source_exists:
                if archived is not None:
                    _fsync_directory(active_parent)
                    _fsync_directory(archived.parent)
                    _fsync_directory(archive_root)
                    return archived
                raise _session_not_found(project_id, session_id)
            if require_resolved_outboxes and session_has_unresolved_outbox(
                source
            ):
                raise LoopMemoryError(
                    code="unresolved_outbox",
                    message="Session outboxes must be resolved before closing",
                )

            ensure_directory(archive_parent)
            source_stat = source.stat()
            archive_time_ns = round(now.timestamp() * 1_000_000_000)
            os.utime(source, ns=(source_stat.st_atime_ns, archive_time_ns))
            _fsync_directory(source)
            try:
                _rename_no_replace(source, destination)
            except OSError as error:
                if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    raise _archive_conflict(destination) from error
                raise
            _fsync_directory(active_parent)
            _fsync_directory(archive_parent)
            _fsync_directory(archive_root)
            return destination


def session_has_unresolved_outbox(session: Path) -> bool:
    if _outbox_has_content(
        session / "agents" / "main" / "outbox.md",
        "# Main Agent Outbox",
    ):
        return True
    subagents = session / "agents" / "subagents"
    try:
        subagents_stat = os.lstat(subagents)
    except OSError:
        return True
    if not stat.S_ISDIR(subagents_stat.st_mode):
        return True
    try:
        agents = list(os.scandir(subagents))
    except OSError:
        return True
    for agent in agents:
        try:
            agent_stat = agent.stat(follow_symlinks=False)
        except OSError:
            return True
        if not stat.S_ISDIR(agent_stat.st_mode):
            return True
        if _outbox_has_content(
            Path(agent.path) / "outbox.md",
            "# Subagent Outbox",
        ):
            return True
    return False


def _outbox_has_content(path: Path, template_title: str) -> bool:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            return True
        expected = template_title.encode("ascii")
        whitespace = b" \t\n\r\v\f"
        expected_index = 0
        started = False
        try:
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                for byte in chunk:
                    if byte > 0x7F:
                        return True
                    if not started:
                        if byte in whitespace:
                            continue
                        started = True
                    if expected_index < len(expected):
                        if byte != expected[expected_index]:
                            return True
                        expected_index += 1
                    elif byte not in whitespace:
                        return True
        except OSError:
            return True
    finally:
        os.close(descriptor)
    return started and expected_index != len(expected)


def _session_template(kind: str, agent_id: str | None) -> str:
    if kind in ("status", "handoff"):
        return _SESSION_TEMPLATES[f"{kind}.md"]
    if agent_id is None:
        return _MAIN_AGENT_TEMPLATES[f"{kind}.md"]
    return f"# Subagent {kind.title()}\n"


def _canonical_root(loop_root: Path) -> Path:
    return Path(loop_root).expanduser().resolve(strict=False)


def _target(root: Path, first: Path | str, *parts: str) -> Path:
    candidate = Path(first)
    if not candidate.is_absolute():
        candidate = root / candidate
    if parts:
        candidate = candidate.joinpath(*parts)
    lexical = Path(os.path.abspath(candidate))
    try:
        resolved = assert_loop_path(root, lexical)
    except LoopMemoryError as error:
        if error.code != "path_outside_loop_root":
            raise
        try:
            lexical.relative_to(root)
        except ValueError:
            raise
        raise LoopMemoryError(
            code="unsafe_path",
            message=f"Derived memory path traverses a symlink: {lexical}",
        ) from error
    if lexical != resolved:
        raise LoopMemoryError(
            code="unsafe_path",
            message=f"Derived memory path traverses a symlink: {lexical}",
        )
    return resolved


def _validate_id(value: str, prefix: str, kind: str) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise LoopMemoryError(
            code=f"invalid_{kind}_id",
            message=f"{kind.title()} ID must be a safe {prefix}-... identifier",
        )
    if not value.startswith(f"{prefix}-"):
        raise LoopMemoryError(
            code=f"invalid_{kind}_id",
            message=f"{kind.title()} ID must begin with {prefix}-",
        )


def _validate_agent_id(agent_id: str) -> None:
    if (
        not isinstance(agent_id, str)
        or agent_id in (".", "..")
        or not _AGENT_ID_PATTERN.fullmatch(agent_id)
    ):
        raise LoopMemoryError(
            code="invalid_agent_id",
            message="Agent ID must be one safe path component",
        )


def _ensure_global_layout(root: Path) -> None:
    ensure_directory(root)
    global_dir = _target(root, "global")
    locks_dir = _target(root, "locks")
    ensure_directory(global_dir)
    ensure_directory(locks_dir)
    for name, template in _GLOBAL_TEMPLATES.items():
        _ensure_file(_target(root, global_dir, name), template)
    global_facts.ensure_facts_layout(root)


def _ensure_project_layout(root: Path, project_id: str) -> Path:
    _ensure_global_layout(root)
    projects_dir = _target(root, "projects")
    project_dir = _target(root, projects_dir, project_id)
    sessions_dir = _target(root, project_dir, "sessions")
    active_dir = _target(root, sessions_dir, "active")
    archive_dir = _target(root, sessions_dir, "archive")
    for directory in (
        projects_dir,
        project_dir,
        sessions_dir,
        active_dir,
        archive_dir,
    ):
        ensure_directory(directory)
    _ensure_file(_target(root, project_dir, "project.md"), _PROJECT_TEMPLATE)
    return project_dir


def _ensure_active_session_layout(
    root: Path,
    project_dir: Path,
    session_id: str,
    *,
    materialize_files: bool = True,
) -> Path:
    session_dir = _target(
        root,
        project_dir,
        "sessions",
        "active",
        session_id,
    )
    agents_dir = _target(root, session_dir, "agents")
    main_dir = _target(root, agents_dir, "main")
    subagents_dir = _target(root, agents_dir, "subagents")
    for directory in (session_dir, agents_dir, main_dir, subagents_dir):
        ensure_directory(directory)
    if materialize_files:
        for name, template in _SESSION_TEMPLATES.items():
            _ensure_file(_target(root, session_dir, name), template)
        for name, template in _MAIN_AGENT_TEMPLATES.items():
            _ensure_file(_target(root, main_dir, name), template)
    return session_dir


class _SessionLifecycleFlock:
    def __init__(self, sessions_dir: Path, operation: int) -> None:
        self.sessions_dir = sessions_dir
        self.operation = operation
        self.descriptor: int | None = None

    def __enter__(self) -> "_SessionLifecycleFlock":
        descriptor = os.open(self.sessions_dir, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, self.operation)
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


def _assert_session_not_archived(
    root: Path,
    sessions_dir: Path,
    session_id: str,
) -> None:
    archived = _find_archived_session(root, sessions_dir, session_id)
    if archived is not None:
        raise LoopMemoryError(
            code="session_archived",
            message=f"Session is already archived: {archived}",
        )


def _find_archived_session(
    root: Path,
    sessions_dir: Path,
    session_id: str,
) -> Path | None:
    archive_root = _target(root, sessions_dir, "archive")
    if not _real_directory_exists(
        archive_root,
        session_id,
        "archive root",
    ):
        return None

    archived: list[Path] = []
    for entry in archive_root.iterdir():
        if not _ARCHIVE_MONTH_PATTERN.fullmatch(entry.name):
            continue
        month_dir = _target(root, archive_root, entry.name)
        if not _real_directory_exists(
            month_dir,
            session_id,
            "archive month",
        ):
            continue
        candidate = _target(root, month_dir, session_id)
        if _real_directory_exists(
            candidate,
            session_id,
            "archived session",
        ):
            archived.append(candidate)

    if len(archived) > 1:
        raise _corrupt_archives(
            session_id,
            f"multiple archived copies exist: {', '.join(map(str, archived))}",
        )
    return archived[0] if archived else None


def _real_directory_exists(
    path: Path,
    session_id: str,
    description: str,
) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(value.st_mode):
        raise _corrupt_archives(
            session_id,
            f"{description} is not a real directory: {path}",
        )
    return True


def _corrupt_archives(session_id: str, reason: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_state",
        message=f"Archive state for {session_id} is corrupt ({reason})",
        recoverable=False,
    )


def _ensure_file(path: Path, template: str) -> None:
    with _SessionLifecycleFlock(path.parent, fcntl.LOCK_EX):
        _ensure_file_locked(path, template)


def _ensure_file_locked(path: Path, template: str) -> None:
    try:
        stream = path.open("xb")
    except FileExistsError:
        _validate_existing_file(path)
        return
    created_identity: tuple[int, int] | None = None
    try:
        try:
            created_identity = _file_identity(os.fstat(stream.fileno()))
        except BaseException:
            try:
                created_identity = _file_identity(os.stat(stream.fileno()))
            except BaseException:
                pass
            raise
        remaining = memoryview(template.encode("utf-8"))
        while remaining:
            written = stream.write(remaining)
            if written == 0:
                raise OSError(errno.EIO, "template write made no progress", path)
            remaining = remaining[written:]
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        _fsync_directory(path.parent)
    except BaseException as primary_error:
        cleanup_error: BaseException | None = None
        if stream is not None:
            try:
                stream.close()
            except BaseException as error:
                cleanup_error = error
        try:
            if created_identity is not None:
                _unlink_if_identity(path, created_identity)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        try:
            _fsync_directory(path.parent)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            raise primary_error from cleanup_error
        raise


def _unlink_if_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        current = _file_identity(os.lstat(path))
    except FileNotFoundError:
        return False
    if current != expected:
        return False
    path.unlink()
    return True


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _validate_existing_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise LoopMemoryError(
            code="invalid_layout_target",
            message=f"Expected a regular memory file: {path}",
        )


def _validate_promotion_section(scope: str, section: str) -> None:
    if scope == "project":
        valid = section in PROJECT_SECTIONS
    elif scope.startswith("project-"):
        valid = section == "Entries"
    elif scope == "global-long":
        valid = section == "Methodology"
    else:
        valid = section == "Entries"
    if not valid:
        raise LoopMemoryError(
            code="invalid_section",
            message=f"Section {section!r} is not valid for {scope}",
        )


def _normalize_entry(entry: str) -> tuple[str, str]:
    if not isinstance(entry, str):
        raise LoopMemoryError(
            code="invalid_entry",
            message="Promotion entry must be text",
        )
    normalized_line_endings = entry.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        _TRAILING_HORIZONTAL_SPACE.sub("", line)
        for line in normalized_line_endings.split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise LoopMemoryError(code="invalid_entry", message="Promotion entry is empty")

    match = _ENTRY_FIRST_LINE.fullmatch(lines[0])
    if match is None:
        raise LoopMemoryError(
            code="invalid_entry",
            message="Promotion entry has an invalid first line",
        )
    try:
        parsed_date = datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError as error:
        raise LoopMemoryError(
            code="invalid_entry",
            message="Promotion entry date is invalid",
        ) from error
    if parsed_date.strftime("%Y-%m-%d") != match.group(1):
        raise LoopMemoryError(
            code="invalid_entry",
            message="Promotion entry date is invalid",
        )

    evidence_found = False
    for line in lines[1:]:
        if line.lstrip().startswith("#") or (line and not line.startswith("  ")):
            raise LoopMemoryError(
                code="invalid_entry",
                message="Promotion entry continuation lines must be indented",
            )
        if line.startswith("  Evidence:"):
            evidence = line[len("  Evidence:") :]
            if evidence.startswith(" ") and evidence.strip():
                evidence_found = True
    if not evidence_found:
        raise LoopMemoryError(
            code="invalid_entry",
            message="Promotion entry must include nonempty indented evidence",
        )
    return "\n".join(lines) + "\n", match.group(2)


def _validate_promotion_status(scope: str, section: str, status: str) -> None:
    if status == "inferred" and scope in ("project", "project-long", "global-long"):
        raise LoopMemoryError(
            code="inferred_not_durable",
            message=f"Inferred entries cannot be promoted to {scope}",
        )
    if scope != "project":
        return
    if status == "superseded" and section != "Superseded":
        raise LoopMemoryError(
            code="invalid_entry_status",
            message="Superseded entries must target the Superseded section",
        )
    if status != "superseded" and section == "Superseded":
        raise LoopMemoryError(
            code="invalid_entry_status",
            message="Only superseded entries may target the Superseded section",
        )


def _read_destination(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as error:
        raise LoopMemoryError(
            code="memory_file_not_found",
            message=f"Memory file does not exist: {path}",
        ) from error


def _find_section(content: bytes, section: str, path: Path) -> tuple[int, int]:
    target = f"## {section}".encode("utf-8")
    records: list[tuple[int, int, bytes]] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        body = _without_line_ending(line)
        records.append((offset, offset + len(line), body))
        offset += len(line)

    matches = [index for index, (_, _, body) in enumerate(records) if body == target]
    if len(matches) != 1:
        raise LoopMemoryError(
            code="invalid_section_heading",
            message=f"Memory file must contain exactly one {target.decode()!r}: {path}",
        )

    heading_index = matches[0]
    section_start = records[heading_index][1]
    section_end = len(content)
    for start, _, body in records[heading_index + 1 :]:
        if body.startswith(b"## "):
            section_end = start
            break
    return section_start, section_end


def _without_line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith((b"\n", b"\r")):
        return line[:-1]
    return line


def _contains_normalized_entry(
    section_content: bytes,
    normalized_entry: str,
    path: Path,
) -> bool:
    try:
        section_text = section_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _corrupt_memory_file(path, "file is not UTF-8") from error
    section_lines = [
        _TRAILING_HORIZONTAL_SPACE.sub("", line)
        for line in section_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    entry_lines = normalized_entry[:-1].split("\n")
    width = len(entry_lines)
    for index in range(len(section_lines) - width + 1):
        if section_lines[index : index + width] != entry_lines:
            continue
        after_index = index + width
        after_is_boundary = (
            after_index == len(section_lines)
            or not section_lines[after_index]
            or section_lines[after_index].startswith("- ")
        )
        if after_is_boundary:
            return True
    return False


def _entry_prefix(before: bytes) -> bytes:
    normalized_endings = before.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if normalized_endings.endswith(b"\n\n"):
        return b""
    if normalized_endings.endswith(b"\n"):
        return b"\n"
    return b"\n\n"


def _corrupt_memory_file(path: Path, reason: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="corrupt_memory_file",
        message=f"Memory file is corrupt ({reason}): {path}",
        recoverable=False,
    )


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
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise _archive_conflict(destination)
    if error_number in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL):
        raise _unsupported_atomic_rename()
    raise OSError(error_number, os.strerror(error_number), destination)


def _archive_conflict(destination: Path) -> LoopMemoryError:
    return LoopMemoryError(
        code="archive_conflict",
        message=f"Archive destination already exists: {destination}",
    )


def _session_not_found(project_id: str, session_id: str) -> LoopMemoryError:
    return LoopMemoryError(
        code="session_not_found",
        message=f"Active session does not exist: {project_id}/{session_id}",
    )


def _unsupported_atomic_rename() -> LoopMemoryError:
    return LoopMemoryError(
        code="archive_atomic_rename_unsupported",
        message="Atomic no-replace directory rename is unavailable on this platform",
        recoverable=False,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
