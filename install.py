#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Iterable
from uuid import uuid4


SOURCE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = SOURCE_ROOT / "runtime"
BEGIN_MARKER = "<!-- BEGIN LOOP ENGINEERING MANAGED -->"
END_MARKER = "<!-- END LOOP ENGINEERING MANAGED -->"
SKILL_NAMES = (
    "managing-loop-memory",
    "governing-subagents",
    "governing-task-scope",
)
CACHE_NAMES = {"__pycache__", ".DS_Store"}

if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from scripts.loopmem.configuration import (  # noqa: E402
    merge_codex_hooks,
    merge_codex_config,
    merge_codex_permission_profile,
    merge_codex_writable_root,
    remove_codex_hooks,
    remove_codex_permission_profile,
    remove_codex_writable_root,
    serialise_json,
)


class InstallerError(RuntimeError):
    def __init__(self, code: str, path: Path | None = None):
        super().__init__(code)
        self.code = code
        self.path = path


def _block_bounds(text: str) -> tuple[int, int] | None:
    begin_count = text.count(BEGIN_MARKER)
    end_count = text.count(END_MARKER)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise InstallerError("ambiguous_agents_markers")
    begin = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < begin:
        raise InstallerError("ambiguous_agents_markers")
    return begin, end + len(END_MARKER)


def _render_block(payload: str) -> str:
    return BEGIN_MARKER + "\n" + payload.rstrip() + "\n" + END_MARKER


def merge_managed_block(existing: str, payload: str) -> str:
    bounds = _block_bounds(existing)
    rendered = _render_block(payload)
    if bounds is not None:
        start, end = bounds
        return existing[:start] + rendered + existing[end:]
    if not existing:
        return rendered + "\n"
    # A previous installer version could publish the exact managed payload
    # without markers. Converge that unambiguous case to one managed block;
    # any other unmarked content remains user-owned and is preserved.
    if existing.strip() == payload.strip():
        return rendered + "\n"
    separator = "\n" if existing.endswith("\n") else "\n\n"
    return existing + separator + rendered + "\n"


def remove_managed_block(existing: str) -> str:
    bounds = _block_bounds(existing)
    if bounds is None:
        return existing
    start, end = bounds
    if start >= 2 and existing[start - 2:start] == "\n\n":
        start -= 1
    suffix_start = end + 1 if existing[end:end + 1] == "\n" else end
    return existing[:start] + existing[suffix_start:]


def _package_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or path.name in CACHE_NAMES:
            continue
        if path.is_file() and path.suffix != ".pyc":
            yield path


def validate_package(root: Path) -> None:
    required = (
        root / "runtime/scripts/loop_memory.py",
        root / "runtime/bin/loop-memory",
        root / "skills/managing-loop-memory/SKILL.md",
        root / "skills/governing-subagents/SKILL.md",
        root / "skills/governing-task-scope/SKILL.md",
        root / "skills/governing-task-scope/scripts/scope_guard.py",
        root / "global/AGENTS.loop-engineering.md",
        root / "global/global-long-methodology.md",
    )
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise InstallerError("package_incomplete", missing[0])
    forbidden_trees = (
        root / "skills/initializing-loop-memory",
        root / "skills/updating-loop-memory",
        root / "projects",
        root / "sessions",
        root / "migrations",
    )
    for path in forbidden_trees:
        if path.exists():
            raise InstallerError("package_contains_unowned_state", path)
    builder_home = str(Path.home().resolve())
    machine_home = re.compile(r"/(?:Users|home)/[^/\s'\"<>]+(?:/|\b)")
    for path in _package_files(root):
        if path.name == ".DS_Store" or path.suffix == ".pyc":
            raise InstallerError("package_contains_cache", path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if builder_home in text or machine_home.search(text):
            raise InstallerError("package_contains_machine_path", path)


@dataclass(frozen=True)
class Target:
    key: str
    path: Path
    staged: Path
    kind: str


def _ignored(path: Path) -> bool:
    return (
        any(part == "__pycache__" for part in path.parts)
        or path.suffix == ".pyc"
        or path.name == ".DS_Store"
    )


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _ignored(relative):
            continue
        kind = b"d" if path.is_dir() else b"f"
        digest.update(kind + b"\0" + relative.as_posix().encode("utf-8") + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _copy_runtime(source: Path, destination: Path) -> None:
    excluded = {
        "tests",
        "agents",
        "__pycache__",
    }

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in excluded or name.endswith(".pyc")}

    shutil.copytree(source, destination, ignore=ignore)


def _copy_skill(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def _read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file():
        raise InstallerError("unsafe_target", path)
    return path.read_text(encoding="utf-8")


def stage_install(home: Path, stage: Path) -> tuple[list[Target], dict[str, object]]:
    validate_package(SOURCE_ROOT)
    stage.mkdir(parents=True, exist_ok=False)
    files = stage / "files"
    trees = stage / "trees"
    files.mkdir()
    trees.mkdir()

    runtime = trees / "runtime"
    _copy_runtime(RUNTIME_ROOT, runtime)
    launcher = files / "loop-memory"
    shutil.copy2(RUNTIME_ROOT / "bin/loop-memory", launcher)

    skill_stages: dict[str, Path] = {}
    for name in SKILL_NAMES:
        destination = trees / name
        _copy_skill(SOURCE_ROOT / "skills" / name, destination)
        skill_stages[name] = destination

    codex = home / ".codex"
    agents_text = _read_text(codex / "AGENTS.md")
    payload = (SOURCE_ROOT / "global/AGENTS.loop-engineering.md").read_text(
        encoding="utf-8"
    )
    agents = files / "AGENTS.md"
    agents.write_text(merge_managed_block(agents_text, payload), encoding="utf-8")

    config_text = _read_text(codex / "config.toml")
    config = files / "config.toml"
    try:
        merged_config_text = merge_codex_config(config_text)
    except ValueError as error:
        raise InstallerError(str(error), codex / "config.toml") from error
    config.write_text(merged_config_text, encoding="utf-8")

    hooks_text = _read_text(codex / "hooks.json", "{}\n")
    try:
        hook_value = json.loads(hooks_text)
    except json.JSONDecodeError as error:
        raise InstallerError("invalid_codex_hooks", codex / "hooks.json") from error
    if not isinstance(hook_value, dict):
        raise InstallerError("invalid_codex_hooks", codex / "hooks.json")
    hooks = files / "hooks.json"
    hooks.write_text(serialise_json(merge_codex_hooks(hook_value)), encoding="utf-8")

    targets = [
        Target(
            ".local/share/loop-memory",
            home / ".local/share/loop-memory",
            runtime,
            "tree",
        ),
        Target(
            ".local/bin/loop-memory",
            home / ".local/bin/loop-memory",
            launcher,
            "file",
        ),
        Target(
            ".codex/skills/managing-loop-memory",
            home / ".codex/skills/managing-loop-memory",
            skill_stages["managing-loop-memory"],
            "tree",
        ),
        Target(
            ".codex/skills/governing-subagents",
            home / ".codex/skills/governing-subagents",
            skill_stages["governing-subagents"],
            "tree",
        ),
        Target(
            ".codex/skills/governing-task-scope",
            home / ".codex/skills/governing-task-scope",
            skill_stages["governing-task-scope"],
            "tree",
        ),
        Target(".codex/AGENTS.md", codex / "AGENTS.md", agents, "file"),
        Target(".codex/config.toml", codex / "config.toml", config, "file"),
        Target(".codex/hooks.json", codex / "hooks.json", hooks, "file"),
    ]
    managed = {
        target.key: {
            "kind": target.kind,
            "sha256": (
                tree_digest(target.staged)
                if target.kind == "tree"
                else file_digest(target.staged)
            ),
        }
        for target in targets
    }
    original_config = tomllib.loads(config_text)
    profile_metadata: dict[str, object] = {
        "name": "loop-memory",
        "had_default_permissions": "default_permissions" in original_config,
        "previous_default_permissions": original_config.get("default_permissions"),
    }
    existing_manifest_path = home / ".local/state/loop-memory-installer/manifest.json"
    if existing_manifest_path.is_file() and not existing_manifest_path.is_symlink():
        try:
            existing_manifest = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
            existing_profile_metadata = existing_manifest.get("codex_permission_profile")
            if isinstance(existing_profile_metadata, dict):
                if "had_default_permissions" in existing_profile_metadata:
                    profile_metadata["had_default_permissions"] = existing_profile_metadata["had_default_permissions"]
                if "previous_default_permissions" in existing_profile_metadata:
                    profile_metadata["previous_default_permissions"] = existing_profile_metadata["previous_default_permissions"]
        except (OSError, json.JSONDecodeError):
            pass
    manifest: dict[str, object] = {
        "schema_version": 1,
        "managed": managed,
        "codex_permission_profile": profile_metadata,
    }
    manifest_file = files / "manifest.json"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    targets.append(
        Target(
            ".local/state/loop-memory-installer/manifest.json",
            home / ".local/state/loop-memory-installer/manifest.json",
            manifest_file,
            "file",
        )
    )
    return targets, manifest


def _same_target(target: Target) -> bool:
    if target.path.is_symlink() or not target.path.exists():
        return False
    if target.kind == "file":
        return target.path.is_file() and target.path.read_bytes() == target.staged.read_bytes()
    return target.path.is_dir() and tree_digest(target.path) == tree_digest(target.staged)


def _remove_exact(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _backup_targets(targets: list[Target], transaction: Path) -> dict[str, object]:
    values: list[dict[str, object]] = []
    payload = transaction / "targets"
    payload.mkdir(parents=True)
    for index, target in enumerate(targets):
        existed = target.path.exists() or target.path.is_symlink()
        entry: dict[str, object] = {
            "key": target.key,
            "kind": target.kind,
            "existed": existed,
            "backup": None,
        }
        if existed:
            if target.path.is_symlink():
                raise InstallerError("unsafe_target", target.path)
            backup = payload / f"{index:02d}"
            if target.kind == "tree":
                if not target.path.is_dir():
                    raise InstallerError("unsafe_target", target.path)
                shutil.copytree(target.path, backup, symlinks=True)
            else:
                if not target.path.is_file():
                    raise InstallerError("unsafe_target", target.path)
                shutil.copy2(target.path, backup)
            entry["backup"] = str(backup.relative_to(transaction))
        values.append(entry)
    journal: dict[str, object] = {"schema_version": 1, "targets": values}
    (transaction / "transaction.json").write_text(
        json.dumps(journal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return journal


def _publish_target(target: Target) -> None:
    target.path.parent.mkdir(parents=True, exist_ok=True)
    sibling = target.path.parent / f".{target.path.name}.loop-install-{uuid4().hex}"
    try:
        if target.kind == "tree":
            shutil.copytree(target.staged, sibling)
        else:
            shutil.copy2(target.staged, sibling)
        _remove_exact(target.path)
        os.replace(sibling, target.path)
    finally:
        _remove_exact(sibling)


def _restore_targets(home: Path, transaction: Path, journal: dict[str, object]) -> None:
    entries = journal.get("targets")
    if not isinstance(entries, list):
        raise InstallerError("rollback_journal_invalid", transaction)
    for entry in reversed(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise InstallerError("rollback_journal_invalid", transaction)
        target = home / entry["key"]
        _remove_exact(target)
        if entry.get("existed") is not True:
            continue
        backup_value = entry.get("backup")
        kind = entry.get("kind")
        if not isinstance(backup_value, str) or kind not in {"file", "tree"}:
            raise InstallerError("rollback_journal_invalid", transaction)
        backup = transaction / backup_value
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "tree":
            shutil.copytree(backup, target, symlinks=True)
        else:
            shutil.copy2(backup, target)


def install_files(home: Path, fail_after: str | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="loop-memory-stage-") as temporary:
        targets, _manifest = stage_install(home, Path(temporary) / "staged")
        changed = [target for target in targets if not _same_target(target)]
        if not changed:
            return {"changed": False, "trust_review": False, "transaction": None}
        state = home / ".local/state/loop-memory-installer"
        backup_root = state / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ-")
        transaction = backup_root / ("install-" + timestamp + uuid4().hex)
        transaction.mkdir()
        journal = _backup_targets(changed, transaction)
        try:
            for target in changed:
                _publish_target(target)
                if fail_after == target.key:
                    raise InstallerError("injected_publish_failure", target.path)
        except BaseException:
            _restore_targets(home, transaction, journal)
            raise
        hooks_changed = any(target.key == ".codex/hooks.json" for target in changed)
        return {
            "changed": True,
            "trust_review": hooks_changed,
            "transaction": str(transaction),
        }


def _run_json(command: list[str], home: Path, cwd: Path) -> dict[str, object]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["PATH"] = (
        str(home / ".local/bin") + os.pathsep + environment.get("PATH", "")
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise InstallerError("invalid_runtime_json") from error
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        raise InstallerError("runtime_command_failed")
    return payload


def _initialize_memory(home: Path) -> None:
    launcher = home / ".local/bin/loop-memory"
    project = home / ".local/share/loop-memory"
    session = "loop-memory-installer-v1"
    _run_json(
        [
            str(launcher),
            "access-check",
            "--root",
            str(home / "loop-memory"),
            "--json",
        ],
        home,
        home,
    )
    enter = _run_json(
        [
            str(launcher),
            "enter",
            "--cwd",
            str(project),
            "--project-root",
            str(project),
            "--session-id",
            session,
            "--json",
        ],
        home,
        home,
    )
    if enter.get("root") != str(home / "loop-memory"):
        raise InstallerError("unexpected_memory_root")
    methodology = SOURCE_ROOT / "global/global-long-methodology.md"
    _run_json(
        [
            str(launcher),
            "global-organize",
            "--cwd",
            str(project),
            "--thread-id",
            session,
            "--methodology",
            str(methodology),
            "--json",
        ],
        home,
        home,
    )


def _verify_static_install(home: Path) -> None:
    launcher = home / ".local/bin/loop-memory"
    _run_json([str(launcher), "--json", "--help"], home, home)
    agents = (home / ".codex/AGENTS.md").read_text(encoding="utf-8")
    if agents.count(BEGIN_MARKER) != 1 or agents.count(END_MARKER) != 1:
        raise InstallerError("agents_verification_failed")
    config = (home / ".codex/config.toml").read_text(encoding="utf-8")
    try:
        parsed_config = tomllib.loads(config)
    except tomllib.TOMLDecodeError as error:
        raise InstallerError("config_verification_failed") from error
    sandbox = parsed_config.get("sandbox_workspace_write")
    roots = sandbox.get("writable_roots") if isinstance(sandbox, dict) else None
    if (
        not isinstance(roots, list)
        or roots.count("~/loop-memory") != 1
        or merge_codex_writable_root(config) != config
    ):
        raise InstallerError("config_verification_failed")
    permissions = parsed_config.get("permissions")
    profile = permissions.get("loop-memory") if isinstance(permissions, dict) else None
    filesystem = profile.get("filesystem") if isinstance(profile, dict) else None
    if (
        parsed_config.get("default_permissions") != "loop-memory"
        or not isinstance(profile, dict)
        or not isinstance(profile.get("extends"), str)
        or not isinstance(filesystem, dict)
        or filesystem.get("~/loop-memory") != "write"
        or set(filesystem) != {"~/loop-memory"}
        or merge_codex_permission_profile(config) != config
    ):
        raise InstallerError("config_verification_failed")
    hooks = json.loads((home / ".codex/hooks.json").read_text(encoding="utf-8"))
    converged = merge_codex_hooks(hooks)
    if converged != hooks:
        raise InstallerError("hooks_verification_failed")
    manifest_path = home / ".local/state/loop-memory-installer/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    managed = manifest.get("managed") if isinstance(manifest, dict) else None
    if not isinstance(managed, dict):
        raise InstallerError("manifest_verification_failed", manifest_path)
    for key, metadata in managed.items():
        if not isinstance(key, str) or not isinstance(metadata, dict):
            raise InstallerError("manifest_verification_failed", manifest_path)
        path = home / key
        expected = metadata.get("sha256")
        kind = metadata.get("kind")
        if not isinstance(expected, str) or kind not in {"file", "tree"}:
            raise InstallerError("manifest_verification_failed", manifest_path)
        actual = file_digest(path) if kind == "file" else tree_digest(path)
        if actual != expected:
            raise InstallerError("manifest_verification_failed", path)


def run_install(home: Path, fail_after: str | None = None) -> dict[str, object]:
    home = home.resolve()
    memory_existed = (home / "loop-memory").exists()
    published = install_files(home, fail_after=fail_after)
    try:
        if not memory_existed:
            _initialize_memory(home)
        _verify_static_install(home)
    except BaseException:
        transaction_value = published.get("transaction")
        if isinstance(transaction_value, str):
            transaction = Path(transaction_value)
            journal = json.loads(
                (transaction / "transaction.json").read_text(encoding="utf-8")
            )
            _restore_targets(home, transaction, journal)
        raise
    return {
        "ok": True,
        "changed": published["changed"],
        "memory_initialized": not memory_existed,
        "codex_trust_review": (
            "required" if published["trust_review"] else "unchanged"
        ),
    }


def _load_manifest(home: Path) -> dict[str, object]:
    path = home / ".local/state/loop-memory-installer/manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallerError("manifest_unavailable", path) from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("managed"), dict)
    ):
        raise InstallerError("manifest_invalid", path)
    return value


def _verify_removable_managed_files(
    home: Path, manifest: dict[str, object]
) -> None:
    managed = manifest["managed"]
    assert isinstance(managed, dict)
    guarded = (
        ".local/share/loop-memory",
        ".local/bin/loop-memory",
        ".codex/skills/managing-loop-memory",
        ".codex/skills/governing-subagents",
        ".codex/skills/governing-task-scope",
    )
    for key in guarded:
        metadata = managed.get(key)
        path = home / key
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("sha256"), str
        ):
            raise InstallerError("manifest_invalid", path)
        if not path.exists():
            continue
        kind = metadata.get("kind")
        if kind == "tree":
            if not path.is_dir():
                raise InstallerError("managed_tree_modified", path)
            actual = tree_digest(path)
        elif kind == "file":
            if not path.is_file():
                raise InstallerError("managed_file_modified", path)
            actual = file_digest(path)
        else:
            raise InstallerError("manifest_invalid", path)
        if actual != metadata["sha256"]:
            raise InstallerError("managed_tree_modified", path)


def _verify_upgrade_managed_files(
    home: Path, manifest: dict[str, object]
) -> None:
    managed = manifest["managed"]
    assert isinstance(managed, dict)
    required = {
        ".local/share/loop-memory",
        ".local/bin/loop-memory",
    }
    if not required.issubset(managed):
        raise InstallerError(
            "manifest_invalid",
            home / ".local/state/loop-memory-installer/manifest.json",
        )
    def is_guarded_skill(key: str) -> bool:
        parts = PurePosixPath(key).parts
        return (
            len(parts) == 3
            and parts[:2] == (".codex", "skills")
            and parts[2] not in {"", ".", ".."}
        )

    guarded = sorted(
        key
        for key in managed
        if key in required or is_guarded_skill(key)
    )
    for key in guarded:
        metadata = managed.get(key)
        path = home / key
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("sha256"), str
        ):
            raise InstallerError("manifest_invalid", path)
        if not path.exists():
            continue
        kind = metadata.get("kind")
        if kind == "tree":
            if path.is_symlink() or not path.is_dir():
                raise InstallerError("managed_tree_modified", path)
            actual = tree_digest(path)
            error_code = "managed_tree_modified"
        elif kind == "file":
            if path.is_symlink() or not path.is_file():
                raise InstallerError("managed_file_modified", path)
            actual = file_digest(path)
            error_code = "managed_file_modified"
        else:
            raise InstallerError("manifest_invalid", path)
        if actual != metadata["sha256"]:
            raise InstallerError(error_code, path)


def run_upgrade(home: Path) -> dict[str, object]:
    home = home.resolve()
    manifest_path = home / ".local/state/loop-memory-installer/manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise InstallerError("upgrade_requires_installation", manifest_path)
    manifest = _load_manifest(home)
    _verify_upgrade_managed_files(home, manifest)
    result = run_install(home)
    return {
        "ok": True,
        "changed": result["changed"],
        "memory_preserved": True,
        "codex_trust_review": result["codex_trust_review"],
    }


def stage_uninstall(home: Path, stage: Path) -> tuple[list[Target], list[Path]]:
    manifest = _load_manifest(home)
    _verify_removable_managed_files(home, manifest)
    stage.mkdir(parents=True, exist_ok=False)
    files = stage / "files"
    files.mkdir()
    codex = home / ".codex"
    profile_metadata = manifest.get("codex_permission_profile")
    previous_default: object | None = None
    has_previous_default = False
    if isinstance(profile_metadata, dict):
        if profile_metadata.get("had_default_permissions") is True:
            previous_default = profile_metadata.get("previous_default_permissions")
            has_previous_default = True
        elif profile_metadata.get("had_default_permissions") is False:
            previous_default = None
            has_previous_default = True

    rewrite: list[Target] = []
    if (codex / "AGENTS.md").exists():
        agents = files / "AGENTS.md"
        agents.write_text(
            remove_managed_block(_read_text(codex / "AGENTS.md")),
            encoding="utf-8",
        )
        rewrite.append(Target(".codex/AGENTS.md", codex / "AGENTS.md", agents, "file"))
    if (codex / "config.toml").exists():
        config = files / "config.toml"
        current_config = remove_codex_writable_root(_read_text(codex / "config.toml"))
        if has_previous_default:
            current_config = remove_codex_permission_profile(
                current_config,
                previous_default_permissions=previous_default,
            )
        else:
            current_config = remove_codex_permission_profile(current_config)
        config.write_text(current_config, encoding="utf-8")
        rewrite.append(
            Target(".codex/config.toml", codex / "config.toml", config, "file")
        )
    if (codex / "hooks.json").exists():
        try:
            hooks_value = json.loads(_read_text(codex / "hooks.json"))
        except json.JSONDecodeError as error:
            raise InstallerError("invalid_codex_hooks", codex / "hooks.json") from error
        if not isinstance(hooks_value, dict):
            raise InstallerError("invalid_codex_hooks", codex / "hooks.json")
        hooks = files / "hooks.json"
        hooks.write_text(
            serialise_json(remove_codex_hooks(hooks_value)),
            encoding="utf-8",
        )
        rewrite.append(Target(".codex/hooks.json", codex / "hooks.json", hooks, "file"))
    remove = [
        home / ".local/share/loop-memory",
        home / ".local/bin/loop-memory",
        home / ".codex/skills/managing-loop-memory",
        home / ".codex/skills/governing-subagents",
        home / ".codex/skills/governing-task-scope",
        home / ".local/state/loop-memory-installer/manifest.json",
    ]
    return rewrite, remove


def run_uninstall(home: Path) -> dict[str, object]:
    home = home.resolve()
    with tempfile.TemporaryDirectory(prefix="loop-memory-uninstall-") as temporary:
        rewrite, remove = stage_uninstall(home, Path(temporary) / "staged")
        changed = [target for target in rewrite if not _same_target(target)]
        removal_targets = [
            Target(
                str(path.relative_to(home)),
                path,
                path,
                "tree" if path.is_dir() else "file",
            )
            for path in remove
            if path.exists()
        ]
        all_targets = changed + removal_targets
        if not all_targets:
            return {"ok": True, "changed": False, "memory_preserved": True}
        backup_root = home / ".local/state/loop-memory-installer/backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        transaction = backup_root / ("uninstall-" + uuid4().hex)
        transaction.mkdir()
        journal = _backup_targets(all_targets, transaction)
        try:
            for target in changed:
                _publish_target(target)
            for path in remove:
                _remove_exact(path)
        except BaseException:
            _restore_targets(home, transaction, journal)
            raise
        return {"ok": True, "changed": True, "memory_preserved": True}


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Loop Memory for the current Codex user."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--upgrade", action="store_true")
    actions.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write("ERROR code=python_version_unsupported\n")
        return 2
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        sys.stderr.write("ERROR code=platform_unsupported\n")
        return 2
    arguments = _parse_arguments()
    home = Path.home().resolve()
    try:
        if arguments.uninstall:
            result = run_uninstall(home)
            print(
                "OK action=uninstall changed="
                + str(result["changed"]).lower()
                + " memory_preserved=true"
            )
        elif arguments.upgrade:
            result = run_upgrade(home)
            print(
                "OK action=upgrade changed="
                + str(result["changed"]).lower()
                + " memory_preserved=true codex_trust_review="
                + str(result["codex_trust_review"])
            )
        else:
            result = run_install(home)
            print(
                "OK action=install changed="
                + str(result["changed"]).lower()
                + " memory_initialized="
                + str(result["memory_initialized"]).lower()
                + " codex_trust_review="
                + str(result["codex_trust_review"])
            )
    except InstallerError as error:
        sys.stderr.write("ERROR code=" + error.code + "\n")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
