"""Pure, non-secret configuration merges for staged user integrations.

These helpers operate on already supplied text/objects.  They never discover,
read, or write a user's live product configuration.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import PurePosixPath
from typing import Any


ROOT = "~/loop-memory"
CODEX_PERMISSION_PROFILE = "loop-memory"
CODEX_PERMISSION_PARENT = ":workspace"
CODEX_ADAPTER = "~/.local/share/loop-memory/adapters/codex_hook.py"
CLAUDE_ADAPTER = "~/.local/share/loop-memory/adapters/claude_hook.py"
HOOK_TIMEOUT = 12
# Codex caps synchronous SessionEnd handlers at three seconds.
CODEX_SESSION_END_TIMEOUT = 3
_MISSING = object()


def _normalise_root(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate in {ROOT, "~/loop-memory/", "$HOME/loop-memory", "$HOME/loop-memory/"}:
        return ROOT
    if candidate.startswith("~/"):
        normal = str(PurePosixPath(candidate))
        if normal == ROOT:
            return ROOT
        return normal
    return candidate


def _normalise_roots(value: object) -> list[str]:
    raw = value if isinstance(value, list) else []
    result: list[str] = []
    for item in raw:
        normal = _normalise_root(item)
        if normal is None or normal == ROOT:
            continue
        if normal not in result:
            result.append(normal)
    result.append(ROOT)
    return result


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_roots(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(item) for item in values) + "]"


_SECTION = re.compile(r"(?m)^\[([^\[\]\n]+)\]\s*(?:#.*)?$")
_KEY_LINE = re.compile(r"(?m)^([ \t]*)([A-Za-z0-9_-]+)([ \t]*)=")


def _normalise_section_name(value: str) -> str:
    return re.sub(r"(['\"])([^'\"]*)\1", r"\2", value.strip())


def _section_bounds(text: str, section: str) -> tuple[int, int] | None:
    matches = list(_SECTION.finditer(text))
    for index, match in enumerate(matches):
        if _normalise_section_name(match.group(1)) == section:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return match.end(), end
    return None


def _replace_toml_key(section_text: str, key: str, replacement: str) -> tuple[str, bool]:
    # Config values we own are scalar or one-line arrays.  A multiline array
    # is handled by the broader bracket span, retaining surrounding sections.
    pattern = re.compile(r"(?ms)^([ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*)\[[^\]]*\]")
    found = pattern.search(section_text)
    if found:
        return section_text[: found.start()] + found.group(1) + replacement + section_text[found.end() :], True
    scalar = re.compile(r"(?m)^([ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*)[^\n]*(?=\n|$)")
    found = scalar.search(section_text)
    if found:
        return section_text[: found.start()] + found.group(1) + replacement + section_text[found.end() :], True
    return section_text, False


def _replace_toml_quoted_key(
    section_text: str, key: str, replacement: str
) -> tuple[str, bool]:
    quoted = "(?:" + re.escape(json.dumps(key)) + "|'" + re.escape(key) + "')"
    pattern = re.compile(
        r"(?m)^([ \t]*" + quoted + r"[ \t]*=[ \t]*)[^\n]*(?=\n|$)"
    )
    found = pattern.search(section_text)
    if found:
        return (
            section_text[: found.start()]
            + found.group(1)
            + replacement
            + section_text[found.end() :],
            True,
        )
    return section_text, False


def _set_top_level_toml_key(text: str, key: str, replacement: str) -> str:
    """Set a scalar before the first table without reserialising user TOML."""
    first_section = _SECTION.search(text)
    prefix_end = first_section.start() if first_section else len(text)
    prefix = text[:prefix_end]
    suffix = text[prefix_end:]
    prefix, found = _replace_toml_key(prefix, key, replacement)
    if found:
        return prefix + suffix
    separator = "" if not prefix or prefix.endswith("\n") else "\n"
    return prefix + separator + f"{key} = {replacement}\n" + suffix


def _append_section(text: str, section: str, body: str) -> str:
    separator = "" if not text or text.endswith("\n") else "\n"
    return text + separator + f"\n[{section}]\n" + body.rstrip() + "\n"


def merge_codex_permission_profile(text: str) -> str:
    """Give every new Codex thread the canonical Loop Memory access profile."""
    import tomllib

    parsed = tomllib.loads(text)
    permissions_value = parsed.get("permissions", _MISSING)
    if permissions_value is _MISSING:
        permissions: dict[str, Any] = {}
    elif isinstance(permissions_value, dict):
        permissions = permissions_value
    else:
        raise ValueError("codex_permission_profile_conflict")
    existing_profile = permissions.get(CODEX_PERMISSION_PROFILE, _MISSING)
    if existing_profile is not _MISSING and not isinstance(existing_profile, dict):
        raise ValueError("codex_permission_profile_conflict")
    if isinstance(existing_profile, dict):
        allowed = {"extends", "filesystem", "network"}
        filesystem = existing_profile.get("filesystem", {})
        network = existing_profile.get("network", {})
        if (
            set(existing_profile) - allowed
            or not isinstance(filesystem, dict)
            or set(filesystem) - {ROOT}
            or (ROOT in filesystem and not isinstance(filesystem[ROOT], str))
            or not isinstance(network, dict)
            or set(network) - {"enabled"}
            or ("enabled" in network and not isinstance(network["enabled"], bool))
            or ("extends" in existing_profile and not isinstance(existing_profile["extends"], str))
        ):
            raise ValueError("codex_permission_profile_conflict")
    legacy = parsed.get("sandbox_workspace_write")
    legacy_network = (
        legacy.get("network_access")
        if isinstance(legacy, dict) and isinstance(legacy.get("network_access"), bool)
        else None
    )

    existing_default = parsed.get("default_permissions", _MISSING)
    if existing_default is not _MISSING and not isinstance(existing_default, str):
        raise ValueError("codex_default_permissions_conflict")
    if isinstance(existing_default, str) and existing_default.startswith(":") and existing_default not in {
        ":workspace", ":read-only"
    }:
        raise ValueError("codex_default_permissions_conflict")
    if (
        isinstance(existing_default, str)
        and not existing_default.startswith(":")
        and existing_default != CODEX_PERMISSION_PROFILE
        and existing_default not in permissions
    ):
        raise ValueError("codex_default_permissions_conflict")
    if (
        isinstance(existing_default, str)
        and not existing_default.startswith(":")
        and existing_default != CODEX_PERMISSION_PROFILE
        and not isinstance(permissions.get(existing_default), dict)
    ):
        raise ValueError("codex_default_permissions_conflict")
    if existing_default == CODEX_PERMISSION_PROFILE and isinstance(existing_profile, dict):
        inherited = existing_profile.get("extends")
        parent = inherited if isinstance(inherited, str) else CODEX_PERMISSION_PARENT
    else:
        parent = (
            existing_default
            if isinstance(existing_default, str)
            else CODEX_PERMISSION_PARENT
        )
    text = _set_top_level_toml_key(
        text,
        "default_permissions",
        _toml_string(CODEX_PERMISSION_PROFILE),
    )

    profile_bounds = _section_bounds(text, "permissions.loop-memory")
    if isinstance(existing_profile, dict) and profile_bounds is None:
        # A dotted or inline profile cannot be edited without reserialising
        # user TOML; fail closed instead of creating a duplicate table.
        raise ValueError("codex_permission_profile_conflict")
    if profile_bounds is None:
        text = _append_section(
            text,
            "permissions.loop-memory",
            f"extends = {_toml_string(parent)}",
        )
    else:
        section_start, section_end = profile_bounds
        section = text[section_start:section_end]
        section, found = _replace_toml_key(
            section, "extends", _toml_string(parent)
        )
        if not found:
            prefix = "" if not section or section.startswith("\n") else "\n"
            section = section + prefix + "extends = " + _toml_string(parent) + "\n"
        text = text[:section_start] + section + text[section_end:]

    filesystem_bounds = _section_bounds(text, "permissions.loop-memory.filesystem")
    if filesystem_bounds is None:
        text = _append_section(
            text,
            "permissions.loop-memory.filesystem",
            f"{_toml_string(ROOT)} = {_toml_string('write')}",
        )
    else:
        section_start, section_end = filesystem_bounds
        section = text[section_start:section_end]
        section, found = _replace_toml_quoted_key(section, ROOT, _toml_string("write"))
        if not found:
            prefix = "" if not section or section.startswith("\n") else "\n"
            section = section + prefix + f"{_toml_string(ROOT)} = {_toml_string('write')}\n"
        text = text[:section_start] + section + text[section_end:]

    if legacy_network is not None:
        current = tomllib.loads(text)
        current_profile = current.get("permissions", {}).get(CODEX_PERMISSION_PROFILE, {})
        current_network = (
            current_profile.get("network")
            if isinstance(current_profile, dict)
            else None
        )
        if isinstance(current_network, dict) and "enabled" in current_network:
            legacy_network = None
    if legacy_network is not None:
        network_bounds = _section_bounds(text, "permissions.loop-memory.network")
        if network_bounds is None:
            text = _append_section(
                text,
                "permissions.loop-memory.network",
                "enabled = " + ("true" if legacy_network else "false"),
            )
        else:
            section_start, section_end = network_bounds
            section = text[section_start:section_end]
            section, found = _replace_toml_key(
                section, "enabled", "true" if legacy_network else "false"
            )
            if not found:
                prefix = "" if not section or section.startswith("\n") else "\n"
                section = section + prefix + "enabled = " + ("true" if legacy_network else "false") + "\n"
            text = text[:section_start] + section + text[section_end:]

    # Parse the final document so malformed nested tables never leave staging.
    tomllib.loads(text)
    return text


def remove_codex_permission_profile(
    text: str, *, previous_default_permissions: object = _MISSING
) -> str:
    """Remove only Loop Memory-owned profile values from Codex TOML."""
    import tomllib

    original = tomllib.loads(text)
    permissions = original.get("permissions", {})
    if permissions is not None and not isinstance(permissions, dict):
        raise ValueError("codex_permission_profile_conflict")
    original_profile = (
        permissions.get(CODEX_PERMISSION_PROFILE)
        if isinstance(permissions, dict)
        else None
    )
    if original_profile is not None and not isinstance(original_profile, dict):
        raise ValueError("codex_permission_profile_conflict")
    parent = (
        original_profile.get("extends")
        if isinstance(original_profile, dict)
        and isinstance(original_profile.get("extends"), str)
        else CODEX_PERMISSION_PARENT
    )

    if (
        isinstance(original_profile, dict)
        and _section_bounds(text, "permissions.loop-memory") is None
    ):
        raise ValueError("codex_permission_profile_conflict")

    for section_name, owned_keys in (
        ("permissions.loop-memory", ("extends",)),
        ("permissions.loop-memory.filesystem", (ROOT,)),
        ("permissions.loop-memory.network", ("enabled",)),
    ):
        bounds = _section_bounds(text, section_name)
        if bounds is None:
            continue
        section_start, section_end = bounds
        section = text[section_start:section_end]
        for key in owned_keys:
            if key == ROOT:
                quoted = "(?:" + re.escape(json.dumps(ROOT)) + "|'" + re.escape(ROOT) + "')"
                pattern = re.compile(r"(?m)^\s*" + quoted + r"\s*=[^\n]*(?:\n|$)")
            else:
                pattern = re.compile(r"(?m)^\s*" + re.escape(key) + r"\s*=[^\n]*(?:\n|$)")
            section = pattern.sub("", section, count=1)
        text = text[:section_start] + section + text[section_end:]

    # Empty owned tables are safe to remove; tables with user values remain.
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    owned_sections = {
        "permissions.loop-memory",
        "permissions.loop-memory.filesystem",
        "permissions.loop-memory.network",
    }
    while index < len(lines):
        match = re.match(r"^\[([^\[\]]+)\]\s*(?:#.*)?$", lines[index].rstrip("\n"))
        if match and _normalise_section_name(match.group(1)) in owned_sections:
            end = index + 1
            while end < len(lines) and not re.match(r"^\[[^\[\]]+\]\s*(?:#.*)?$", lines[end].rstrip("\n")):
                end += 1
            body = "".join(lines[index + 1:end]).strip()
            if not body:
                index = end
                continue
        output.append(lines[index])
        index += 1
    result = "".join(output)
    parsed = tomllib.loads(result)
    if parsed.get("default_permissions") == CODEX_PERMISSION_PROFILE:
        first_section = _SECTION.search(result)
        prefix_end = first_section.start() if first_section else len(result)
        prefix = result[:prefix_end]
        suffix = result[prefix_end:]
        scalar = re.compile(r"(?m)^([ \t]*default_permissions[ \t]*=[ \t]*)[^\n]*(?:\n|$)")
        if previous_default_permissions is None:
            replacement = ""
        elif previous_default_permissions is _MISSING:
            replacement = "default_permissions = " + _toml_string(parent) + "\n"
        elif isinstance(previous_default_permissions, str):
            replacement = "default_permissions = " + _toml_string(previous_default_permissions) + "\n"
        else:
            raise ValueError("invalid_previous_default_permissions")
        result = scalar.sub(replacement, prefix, count=1) + suffix
    tomllib.loads(result)
    return result


def merge_codex_config(text: str) -> str:
    """Set the two required sandbox values while retaining all other TOML."""
    import tomllib

    parsed = tomllib.loads(text)
    table_value = parsed.get("sandbox_workspace_write", _MISSING)
    if table_value is not _MISSING and not isinstance(table_value, dict):
        raise ValueError("codex_sandbox_conflict")
    table = table_value if isinstance(table_value, dict) else {}
    if (
        "network_access" in table
        and not isinstance(table["network_access"], bool)
    ):
        raise ValueError("codex_sandbox_conflict")
    if (
        "writable_roots" in table
        and not isinstance(table["writable_roots"], list)
    ):
        raise ValueError("codex_sandbox_conflict")
    roots = _normalise_roots(table.get("writable_roots"))
    replacement_roots = _toml_roots(roots)
    bounds = _section_bounds(text, "sandbox_workspace_write")
    if bounds is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        legacy = (
            text
            + separator
            + "\n[sandbox_workspace_write]\n"
            + "network_access = true\n"
            + "writable_roots = "
            + replacement_roots
            + "\n"
        )
        return merge_codex_permission_profile(legacy)
    section_start, section_end = bounds
    section = text[section_start:section_end]
    section, roots_found = _replace_toml_key(section, "writable_roots", replacement_roots)
    network_found = bool(re.search(r"(?m)^\s*network_access\s*=", section))
    additions: list[str] = []
    if not network_found:
        additions.append("network_access = true")
    if not roots_found:
        additions.append("writable_roots = " + replacement_roots)
    if additions:
        prefix = "" if not section or section.startswith("\n") else "\n"
        section = section + prefix + "\n".join(additions) + "\n"
    return merge_codex_permission_profile(
        text[:section_start] + section + text[section_end:]
    )


def _set_codex_writable_roots(text: str, roots: list[str]) -> str:
    import tomllib

    tomllib.loads(text)
    replacement = _toml_roots(roots)
    bounds = _section_bounds(text, "sandbox_workspace_write")
    if bounds is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        return (
            text
            + separator
            + "\n[sandbox_workspace_write]\n"
            + "writable_roots = "
            + replacement
            + "\n"
        )
    section_start, section_end = bounds
    section = text[section_start:section_end]
    section, found = _replace_toml_key(section, "writable_roots", replacement)
    if not found:
        prefix = "" if not section or section.startswith("\n") else "\n"
        section = section + prefix + "writable_roots = " + replacement + "\n"
    return text[:section_start] + section + text[section_end:]


def merge_codex_writable_root(text: str) -> str:
    """Add the canonical Loop root without changing network access."""
    import tomllib

    parsed = tomllib.loads(text)
    section = parsed.get("sandbox_workspace_write")
    section = section if isinstance(section, dict) else {}
    return _set_codex_writable_roots(
        text,
        _normalise_roots(section.get("writable_roots")),
    )


def remove_codex_writable_root(text: str) -> str:
    """Remove only canonical Loop-root variants from writable roots."""
    import tomllib

    parsed = tomllib.loads(text)
    section = parsed.get("sandbox_workspace_write")
    if not isinstance(section, dict) or "writable_roots" not in section:
        return text
    raw = section.get("writable_roots")
    values = raw if isinstance(raw, list) else []
    roots: list[str] = []
    for item in values:
        normal = _normalise_root(item)
        if normal is None or normal == ROOT or normal in roots:
            continue
        roots.append(normal)
    return _set_codex_writable_roots(text, roots)


def _hook_timeout(adapter: str, event: str) -> int:
    if adapter == CODEX_ADAPTER and event == "SessionEnd":
        return CODEX_SESSION_END_TIMEOUT
    return HOOK_TIMEOUT


def _hook_entry(adapter: str, timeout: int) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": "python3 " + adapter,
                "timeout": timeout,
            }
        ]
    }


def _is_loop_hook(value: object, adapter: str) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") != "command":
        return False
    command = value.get("command")
    if command == "python3 " + adapter:
        return True
    # A structured argv form is unambiguous and safe to converge if a host
    # already emitted it.  Arbitrary strings containing the adapter path are
    # unrelated and remain byte-for-byte equivalent in the copied structure.
    return command == ["python3", adapter]


def _merge_hooks(value: object, events: tuple[str, ...], adapter: str) -> dict[str, Any]:
    hooks: dict[str, Any] = copy.deepcopy(value) if isinstance(value, dict) else {}
    for event in events:
        timeout = _hook_timeout(adapter, event)
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        first: dict[str, Any] | None = None
        for group in entries:
            if not isinstance(group, dict):
                continue
            nested = group.get("hooks")
            if not isinstance(nested, list):
                continue
            kept: list[object] = []
            for hook in nested:
                if not _is_loop_hook(hook, adapter):
                    kept.append(hook)
                    continue
                if first is None:
                    assert isinstance(hook, dict)
                    hook["command"] = "python3 " + adapter
                    hook["timeout"] = timeout
                    first = hook
                    kept.append(hook)
            group["hooks"] = kept
        if first is None:
            entries.append(_hook_entry(adapter, timeout))
        hooks[event] = entries
    return hooks


def merge_codex_hooks(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["hooks"] = _merge_hooks(result.get("hooks"), ("SessionStart", "SessionEnd", "SubagentStart"), CODEX_ADAPTER)
    return result


def _remove_hooks(value: object, events: tuple[str, ...], adapter: str) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(value) if isinstance(value, dict) else {}
    for event in events:
        entries = result.get(event)
        if not isinstance(entries, list):
            continue
        kept_groups: list[object] = []
        for group in entries:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            copied = copy.deepcopy(group)
            copied["hooks"] = [
                hook for hook in copied["hooks"] if not _is_loop_hook(hook, adapter)
            ]
            if copied["hooks"] or set(copied) != {"hooks"}:
                kept_groups.append(copied)
        if kept_groups:
            result[event] = kept_groups
        else:
            result.pop(event, None)
    return result


def remove_codex_hooks(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    hooks = _remove_hooks(
        result.get("hooks"),
        ("SessionStart", "SessionEnd", "SubagentStart"),
        CODEX_ADAPTER,
    )
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def merge_claude_settings(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["hooks"] = _merge_hooks(result.get("hooks"), ("SessionStart", "SessionEnd"), CLAUDE_ADAPTER)
    return result


def serialise_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
