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
CODEX_ADAPTER = "~/.local/share/loop-memory/adapters/codex_hook.py"
CLAUDE_ADAPTER = "~/.local/share/loop-memory/adapters/claude_hook.py"
HOOK_TIMEOUT = 12
# Codex caps synchronous SessionEnd handlers at three seconds.
CODEX_SESSION_END_TIMEOUT = 3


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


_SECTION = re.compile(r"(?m)^\[([^\[\]\n]+)\]\s*$")
_KEY_LINE = re.compile(r"(?m)^([ \t]*)([A-Za-z0-9_-]+)([ \t]*)=")


def _section_bounds(text: str, section: str) -> tuple[int, int] | None:
    matches = list(_SECTION.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1).strip() == section:
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


def merge_codex_config(text: str) -> str:
    """Set the two required sandbox values while retaining all other TOML."""
    import tomllib

    parsed = tomllib.loads(text)
    table = parsed.get("sandbox_workspace_write")
    table = table if isinstance(table, dict) else {}
    roots = _normalise_roots(table.get("writable_roots"))
    replacement_roots = _toml_roots(roots)
    bounds = _section_bounds(text, "sandbox_workspace_write")
    if bounds is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        return (
            text
            + separator
            + "\n[sandbox_workspace_write]\n"
            + "network_access = true\n"
            + "writable_roots = "
            + replacement_roots
            + "\n"
        )
    section_start, section_end = bounds
    section = text[section_start:section_end]
    section, roots_found = _replace_toml_key(section, "writable_roots", replacement_roots)
    section, network_found = _replace_toml_key(section, "network_access", "true")
    additions: list[str] = []
    if not network_found:
        additions.append("network_access = true")
    if not roots_found:
        additions.append("writable_roots = " + replacement_roots)
    if additions:
        prefix = "" if not section or section.startswith("\n") else "\n"
        section = section + prefix + "\n".join(additions) + "\n"
    return text[:section_start] + section + text[section_end:]


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
