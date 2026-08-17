"""Root authority convergence entry points."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.loopmem.capabilities import Capabilities, Notice
from scripts.loopmem.root import relocate_root


@dataclass(frozen=True)
class ConvergenceCacheKey:
    root_id: str
    root_generation: int
    registry_generation: int
    cwd: str
    project_root: str
    scope_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "root_generation": self.root_generation,
            "registry_generation": self.registry_generation,
            "cwd": self.cwd,
            "project_root": self.project_root,
            "scope_digest": self.scope_digest,
        }

    def matches(self, other: "ConvergenceCacheKey") -> bool:
        return self == other


_SCOPE_CACHE: dict[ConvergenceCacheKey, dict[str, object]] = {}


def cached_scope(key: ConvergenceCacheKey) -> dict[str, object] | None:
    value = _SCOPE_CACHE.get(key)
    return dict(value) if value is not None else None


def remember_scope(key: ConvergenceCacheKey, value: dict[str, object]) -> None:
    _SCOPE_CACHE[key] = dict(value)


def evaluate_capabilities(
    *,
    protected_current_project_legacy: bool = False,
    credential_current_project_legacy: bool = False,
    ambiguous_project_facts: bool = False,
    migration_conflict: bool = False,
    unrelated_project_migration: bool = False,
    unresolved_outbox: bool = False,
    global_long_organization_due: bool = False,
    session_recovered: bool = False,
) -> tuple[Capabilities, tuple[Notice, ...]]:
    """Reduce already-validated scope observations to immutable capabilities."""
    values = Capabilities().as_dict()
    notices: list[Notice] = []
    if protected_current_project_legacy or credential_current_project_legacy:
        blocking = ("project_read", "project_promote", "migration_apply")
        for name in blocking:
            values[name] = False
        notices.append(Notice(
            "protected_current_project_legacy",
            "project",
            blocking,
            "review_current_project_legacy",
        ))
    elif ambiguous_project_facts:
        values["project_promote"] = False
        notices.append(Notice(
            "ambiguous_project_facts",
            "project",
            ("project_promote",),
            "resolve_project_facts",
        ))
    if migration_conflict:
        values["migration_apply"] = False
        notices.append(Notice(
            "migration_conflict",
            "project",
            ("migration_apply",),
            "resolve_migration_conflict",
        ))
    if unrelated_project_migration:
        notices.append(Notice(
            "unrelated_project_migration",
            "other-project",
        ))
    if global_long_organization_due:
        notices.append(Notice(
            "global_long_organization_due",
            "global",
            next_action="global-organize",
        ))
    if unresolved_outbox:
        values["session_close"] = False
        notices.append(Notice(
            "unresolved_outbox",
            "session",
            ("session_close",),
            "resolve_outbox",
        ))
    if session_recovered:
        notices.append(Notice(
            "session_memory_reinitialized",
            "session",
            next_action="continue_with_new_session",
        ))
    return Capabilities(**values), tuple(notices)


def converge_root_authority(
    *,
    old_root: Path,
    new_root: Path,
    fault: Any | None = None,
) -> Path:
    return relocate_root(old_root, new_root, fault=fault)
