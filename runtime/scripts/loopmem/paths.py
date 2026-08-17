from dataclasses import dataclass
import os
from pathlib import Path

from scripts.loopmem.errors import LoopMemoryError


@dataclass(frozen=True)
class ProjectDiscovery:
    kind: str
    cwd: Path
    root: Path
    alias: str | None = None


def default_loop_root() -> Path:
    return Path.home() / "loop-memory"


def legacy_loop_root() -> Path:
    return (Path.home() / ".codex" / "loop-memory").resolve(strict=False)


def assert_loop_path(loop_root: Path, candidate: Path) -> Path:
    resolved_root = loop_root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)

    if resolved_candidate == resolved_root:
        raise LoopMemoryError(
            code="loop_root_not_file_target",
            message=f"Loop root cannot be used as a writable file target: {resolved_root}",
        )

    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise LoopMemoryError(
            code="path_outside_loop_root",
            message=(
                f"Writable target must be inside the loop root {resolved_root}: "
                f"{resolved_candidate}"
            ),
        ) from error

    return resolved_candidate


def is_reserved_product_path(candidate: Path) -> bool:
    lexical_candidate = Path(os.path.abspath(candidate.expanduser()))
    codex_home = Path(os.path.abspath(Path.home() / ".codex"))
    memories_root = codex_home / "memories"

    try:
        lexical_candidate.relative_to(memories_root)
        return True
    except ValueError:
        pass

    if (
        lexical_candidate.parent == codex_home
        and lexical_candidate.name.startswith("memories_1.sqlite")
    ):
        return True

    sqlite_dir = codex_home / "sqlite"
    return (
        lexical_candidate.parent == sqlite_dir
        and lexical_candidate.name.startswith("memories_1.sqlite")
    )


def discover_project(
    cwd: Path,
    project_root: Path | None = None,
) -> ProjectDiscovery:
    resolved_cwd = cwd.expanduser().resolve(strict=False)
    resolved_root = (
        project_root.expanduser().resolve(strict=False)
        if project_root is not None
        else resolved_cwd
    )
    if project_root is not None:
        try:
            resolved_cwd.relative_to(resolved_root)
        except ValueError as error:
            raise LoopMemoryError(
                code="cwd_outside_project_root",
                message="Current directory must be inside the explicit project root",
                recoverable=False,
            ) from error
    return ProjectDiscovery(
        kind="directory",
        cwd=resolved_cwd,
        root=resolved_root,
        alias=None,
    )
