#!/usr/bin/env python3
"""Validate staged syntax and print only non-sensitive structural facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tomllib

CODEX_ADAPTER = "~/.local/share/loop-memory/adapters/codex_hook.py"
CLAUDE_ADAPTER = "~/.local/share/loop-memory/adapters/claude_hook.py"
HOOK_TIMEOUT = 12
# Codex caps synchronous SessionEnd handlers at three seconds.
CODEX_SESSION_END_TIMEOUT = 3


def _loop_counts(value: object, adapter: str, timeout: int) -> tuple[int, int]:
    if not isinstance(value, list):
        return 0, 0
    identity = 0
    canonical = 0
    for group in value:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for hook in group["hooks"]:
            if not isinstance(hook, dict) or hook.get("command") != "python3 " + adapter:
                continue
            identity += 1
            if hook.get("type") == "command" and hook.get("timeout") == timeout:
                canonical += 1
    return identity, canonical


def _without_events(value: dict[str, object], owned: set[str], adapter: str) -> dict[str, object]:
    import copy

    result = copy.deepcopy(value)
    event_map = result.get("hooks")
    if isinstance(event_map, dict):
        for event in owned:
            entries = event_map.get(event)
            if not isinstance(entries, list):
                continue
            normalized_entries = []
            for group in entries:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    normalized_entries.append(group)
                    continue
                copy_group = copy.deepcopy(group)
                remaining = []
                for hook in copy_group["hooks"]:
                    if not (isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == "python3 " + adapter):
                        remaining.append(hook)
                        continue
                    # Keep every non-owned field so source/staged semantic
                    # comparison catches drift in metadata on the Loop hook.
                    owned_hook = {key: value for key, value in hook.items() if key not in {"type", "command", "timeout"}}
                    if owned_hook:
                        remaining.append({"__loop_owned__": True, **owned_hook})
                copy_group["hooks"] = remaining
                if not remaining and set(copy_group) == {"hooks"}:
                    continue
                normalized_entries.append(copy_group)
            if normalized_entries:
                event_map[event] = normalized_entries
            else:
                event_map.pop(event, None)
        if not event_map:
            result.pop("hooks", None)
    return result


def _without_codex_owned(value: dict[str, object]) -> dict[str, object]:
    import copy

    result = copy.deepcopy(value)
    section = result.get("sandbox_workspace_write")
    if isinstance(section, dict):
        section.pop("network_access", None)
        section.pop("writable_roots", None)
        if not section:
            result.pop("sandbox_workspace_write", None)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", required=True, type=Path)
    parser.add_argument("--source-hooks", required=True, type=Path)
    parser.add_argument("--source-settings", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--hooks", required=True, type=Path)
    parser.add_argument("--settings", required=True, type=Path)
    args = parser.parse_args()
    source_config = tomllib.loads(args.source_config.read_text(encoding="utf-8"))
    source_hooks = json.loads(args.source_hooks.read_text(encoding="utf-8"))
    source_settings = json.loads(args.source_settings.read_text(encoding="utf-8"))
    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    hooks = json.loads(args.hooks.read_text(encoding="utf-8"))
    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    config_keys = set(config)
    source_keys = set(source_config)
    if config_keys - source_keys - {"sandbox_workspace_write"} or source_keys - config_keys:
        raise SystemExit("staged Codex top-level key set changed")
    if set(hooks) - set(source_hooks) - {"hooks"} or set(source_hooks) - set(hooks):
        raise SystemExit("staged Codex hook top-level key set changed")
    if set(settings) - set(source_settings) - {"hooks"} or set(source_settings) - set(settings):
        raise SystemExit("staged Claude top-level key set changed")
    roots = config.get("sandbox_workspace_write", {}).get("writable_roots", [])
    if not isinstance(roots, list) or roots.count("~/loop-memory") != 1:
        raise SystemExit("staged config must contain one ~/loop-memory root")
    if any(value in {"~/loop-memory/", "$HOME/loop-memory", "$HOME/loop-memory/"} for value in roots):
        raise SystemExit("staged config contains a noncanonical Loop Memory root")
    if config.get("sandbox_workspace_write", {}).get("network_access") is not True:
        raise SystemExit("staged config must enable network_access")
    if _without_codex_owned(source_config) != _without_codex_owned(config):
        raise SystemExit("unowned Codex configuration drifted")
    expected_codex = {"SessionStart", "SessionEnd", "SubagentStart"}
    expected_claude = {"SessionStart", "SessionEnd"}
    codex_map = hooks.get("hooks", {})
    claude_map = settings.get("hooks", {})
    if not isinstance(codex_map, dict) or not expected_codex.issubset(codex_map):
        raise SystemExit("staged Codex hooks are incomplete")
    if not isinstance(claude_map, dict) or not expected_claude.issubset(claude_map):
        raise SystemExit("staged Claude hooks are incomplete")
    for event in expected_codex:
        timeout = CODEX_SESSION_END_TIMEOUT if event == "SessionEnd" else HOOK_TIMEOUT
        identity, canonical = _loop_counts(codex_map[event], CODEX_ADAPTER, timeout)
        if identity != 1 or canonical != 1:
            raise SystemExit("staged Codex hook contract is invalid")
    for event in expected_claude:
        identity, canonical = _loop_counts(
            claude_map[event], CLAUDE_ADAPTER, HOOK_TIMEOUT
        )
        if identity != 1 or canonical != 1:
            raise SystemExit("staged Claude hook contract is invalid")
    source_hooks_without_owned = _without_events(source_hooks, expected_codex, CODEX_ADAPTER)
    staged_hooks_without_owned = _without_events(hooks, expected_codex, CODEX_ADAPTER)
    if source_hooks_without_owned != staged_hooks_without_owned:
        raise SystemExit("unowned Codex hook configuration drifted")
    source_settings_without_owned = _without_events(source_settings, expected_claude, CLAUDE_ADAPTER)
    staged_settings_without_owned = _without_events(settings, expected_claude, CLAUDE_ADAPTER)
    if source_settings_without_owned != staged_settings_without_owned:
        raise SystemExit("unowned Claude configuration drifted")
    codex_events = sorted(expected_codex)
    claude_events = sorted(expected_claude)
    print(
        "OK root=~/loop-memory codex_hooks=" + ",".join(codex_events)
        + " claude_hooks=" + ",".join(claude_events)
        + " codex_trust_review=required"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
