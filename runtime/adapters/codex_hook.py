#!/usr/bin/env python3
"""Codex lifecycle parser and stdin/stdout entry point."""

import json
import re
import subprocess
import sys
from typing import Callable, Mapping

try:
    from .common import HostEvent, HostEventError, process_event, warning
except ImportError:  # Direct script execution after user-level installation.
    from common import HostEvent, HostEventError, process_event, warning


_START_SOURCES = {"startup", "resume", "clear", "compact"}
_AGENT_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostEventError(code)
    return value


def parse_event(value: Mapping[str, object]) -> HostEvent:
    version = value.get("version", value.get("event_version", 1))
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise HostEventError("unsupported_event_version")
    event_name = _text(value.get("hook_event_name"), "invalid_host_event")
    if event_name not in {"SessionStart", "SessionEnd", "SubagentStart"}:
        raise HostEventError("unsupported_host_event")
    session_id = _text(value.get("session_id"), "missing_host_identity")
    cwd = _text(value.get("cwd"), "missing_host_identity")
    source = None
    reason = None
    agent_id = None
    if event_name == "SessionStart":
        source = _text(value.get("source"), "invalid_event_source")
        if source not in _START_SOURCES:
            raise HostEventError("invalid_event_source")
    elif event_name == "SessionEnd":
        reason = _text(value.get("reason"), "invalid_event_reason")
    else:
        agent_id = _text(value.get("agent_id"), "missing_host_identity")
        if agent_id in {".", ".."} or _AGENT_ID.fullmatch(agent_id) is None:
            raise HostEventError("invalid_agent_id")
    return HostEvent(
        event_name=event_name,
        session_id=session_id,
        cwd=cwd,
        source=source,
        reason=reason,
        agent_id=agent_id,
        version=1,
    )


def handle(
    value: Mapping[str, object],
    *,
    access_approved: Callable[[], bool] | None = None,
) -> dict[str, object]:
    try:
        event = parse_event(value)
    except HostEventError as error:
        event_name = value.get("hook_event_name")
        return warning(error.code, event_name if isinstance(event_name, str) else None)
    return process_event(
        event,
        runner=subprocess.run,
        access_approved=access_approved,
    )


def main() -> int:
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError
        output = handle(value)
    except (json.JSONDecodeError, ValueError):
        output = warning("invalid_host_event")
    json.dump(output, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
