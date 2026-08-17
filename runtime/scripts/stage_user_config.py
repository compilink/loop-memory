#!/usr/bin/env python3
"""Merge sanitized configuration fixtures into a disposable staging tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from scripts.loopmem.configuration import (
        merge_claude_settings,
        merge_codex_config,
        merge_codex_hooks,
        serialise_json,
    )
except ImportError:
    from loopmem.configuration import (
        merge_claude_settings,
        merge_codex_config,
        merge_codex_hooks,
        serialise_json,
    )


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-config", required=True, type=Path)
    parser.add_argument("--codex-hooks", required=True, type=Path)
    parser.add_argument("--claude-settings", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    codex_config = merge_codex_config(args.codex_config.read_text(encoding="utf-8"))
    codex_hooks = merge_codex_hooks(json.loads(args.codex_hooks.read_text(encoding="utf-8")))
    claude_settings = merge_claude_settings(json.loads(args.claude_settings.read_text(encoding="utf-8")))
    (args.output_dir / "config.toml").write_text(codex_config, encoding="utf-8")
    (args.output_dir / "hooks.json").write_text(serialise_json(codex_hooks), encoding="utf-8")
    (args.output_dir / "settings.json").write_text(serialise_json(claude_settings), encoding="utf-8")
    print(
        "OK root=~/loop-memory "
        "codex_hooks=SessionEnd,SessionStart,SubagentStart "
        "claude_hooks=SessionEnd,SessionStart "
        "codex_trust_review=required"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
