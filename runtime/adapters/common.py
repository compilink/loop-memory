"""Host-neutral lifecycle translation for Loop Memory adapters.

This module handles normalized events only.  Host products own their parsers;
the storage CLI remains the sole authority for identity, capabilities and
paths.
"""

from dataclasses import dataclass
import json
import subprocess
from typing import Callable, Mapping, Sequence


ENTER_TIMEOUT_SECONDS = 8.0
CLOSE_TIMEOUT_SECONDS = 4.0
CLI_COMMAND = "loop-memory"

_PATH_CAPABILITIES = {
    "global_long": "global_read",
    "global_medium": "global_read",
    "global_short": "global_read",
    "project_memory": "project_read",
    "status": "session_read",
    "handoff": "session_read",
    "agent_inbox": "session_read",
    "agent_outbox": "session_read",
}
_CAPABILITY_KEYS = (
    "global_read", "global_promote", "project_read", "project_promote",
    "session_read", "session_write", "session_close", "migration_apply",
)


class HostEventError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HostEvent:
    event_name: str
    session_id: str
    cwd: str
    source: str | None = None
    reason: str | None = None
    agent_id: str | None = None
    version: int = 1


def warning(code: str, event_name: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "ok": False,
        "warning": {"code": code, "recoverable": True},
    }
    if event_name in {"SessionStart", "SessionEnd", "SubagentStart"}:
        value["hookSpecificOutput"] = {
            "hookEventName": event_name,
            "additionalContext": json.dumps(
                {"loop_memory": {"ok": False, "warning": code}},
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
    return value


def enter_command(event: HostEvent) -> list[str]:
    command = [
        CLI_COMMAND,
        "enter",
        "--cwd",
        event.cwd,
        "--session-id",
        event.session_id,
        "--project-root",
        event.cwd,
    ]
    if event.agent_id is not None:
        command.extend(("--agent-id", event.agent_id))
    command.append("--json")
    return command


def close_command(event: HostEvent) -> list[str]:
    return [
        CLI_COMMAND,
        "session-close",
        "--cwd",
        event.cwd,
        "--thread-id",
        event.session_id,
        "--json",
    ]


def _run_json(
    command: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
    timeout: float,
) -> tuple[subprocess.CompletedProcess[str], Mapping[str, object]]:
    completed = runner(
        list(command),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    try:
        value = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        raise HostEventError("invalid_cli_response")
    if not isinstance(value, dict):
        raise HostEventError("invalid_cli_response")
    return completed, value


def _additional_context(payload: Mapping[str, object]) -> str:
    raw_capabilities = payload.get("capabilities")
    capabilities = {
        key: raw_capabilities[key]
        for key in _CAPABILITY_KEYS
        if isinstance(raw_capabilities, dict) and raw_capabilities.get(key) is True
    }
    notices: list[dict[str, object]] = []
    raw_notices = payload.get("notices")
    if isinstance(raw_notices, list):
        for item in raw_notices:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            scope = item.get("scope")
            blocking = item.get("blocking")
            if not isinstance(code, str) or not isinstance(scope, str):
                continue
            if not isinstance(blocking, list) or not all(isinstance(v, str) for v in blocking):
                continue
            notice: dict[str, object] = {
                "code": code,
                "scope": scope,
                "blocking": list(blocking),
            }
            next_action = item.get("next_action")
            if isinstance(next_action, str):
                notice["next_action"] = next_action
            notices.append(notice)
    paths = payload.get("paths")
    selected: dict[str, str] = {}
    if isinstance(paths, dict):
        for name, capability in _PATH_CAPABILITIES.items():
            value = paths.get(name)
            if capabilities.get(capability) is True and isinstance(value, str):
                selected[name] = value
    resume_handoff = payload.get("resume_handoff")
    if capabilities.get("session_read") is True and isinstance(resume_handoff, str):
        selected["resume_handoff"] = resume_handoff
    context = {
        "project_id": payload.get("project_id"),
        "session_id": payload.get("session_id"),
        "capabilities": capabilities,
        "notices": notices,
        "context_paths": selected,
    }
    return json.dumps(
        {"loop_memory": context},
        separators=(",", ":"),
        sort_keys=True,
    )


def _success_output(event: HostEvent, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event.event_name,
            "additionalContext": _additional_context(payload),
        }
    }


def _access_output(event: HostEvent, payload: Mapping[str, object]) -> dict[str, object]:
    required = payload.get("required_access")
    exact = (
        isinstance(required, dict)
        and required.get("path") == "~/loop-memory"
        and required.get("read") is True
        and required.get("write") is True
        and required.get("execute") is False
    )
    if not exact:
        message = (
            "Loop Memory access is unavailable; no safe access request could be "
            "derived. Loop Memory writes, promotion, migration, and irreversible "
            "external side effects are blocked until enter succeeds; read-only "
            "diagnosis and recoverable local work may continue."
        )
        context = {
            "loop_memory": {
                "ok": False,
                "blocked": True,
                "reason": "environment_access_denied",
                "block_scope": "trusted_state_writes_and_irreversible_external_side_effects",
                "allowed_actions": [
                    "read_only_diagnosis",
                    "recoverable_local_work",
                ],
                "next_action": "stop_and_report",
            }
        }
    else:
        message = (
            "Loop Memory requires read/write access to ~/loop-memory. Grant that "
            "access, then retry once. Loop Memory writes, promotion, migration, "
            "and irreversible external side effects are blocked until enter "
            "succeeds; read-only diagnosis and recoverable local work may continue."
        )
        context = {
            "loop_memory": {
                "ok": False,
                "blocked": True,
                "reason": "environment_access_denied",
                "block_scope": "trusted_state_writes_and_irreversible_external_side_effects",
                "allowed_actions": [
                    "read_only_diagnosis",
                    "recoverable_local_work",
                ],
                "required_access": {
                    "path": "~/loop-memory",
                    "read": True,
                    "write": True,
                    "execute": False,
                },
                "next_action": "request_environment_access",
            }
        }
    return {
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": event.event_name,
            "additionalContext": json.dumps(
                context, separators=(",", ":"), sort_keys=True
            ),
        },
    }


def _is_access_denial(payload: Mapping[str, object]) -> bool:
    error = payload.get("error")
    return (
        isinstance(error, dict)
        and error.get("code") == "environment_access_denied"
    )


def _has_exact_access_request(payload: Mapping[str, object]) -> bool:
    required = payload.get("required_access")
    return (
        isinstance(required, dict)
        and required.get("path") == "~/loop-memory"
        and required.get("read") is True
        and required.get("write") is True
        and required.get("execute") is False
    )


def _enter(
    event: HostEvent,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    access_approved: Callable[[], bool] | None,
) -> tuple[subprocess.CompletedProcess[str], Mapping[str, object]]:
    completed, payload = _run_json(
        enter_command(event), runner, ENTER_TIMEOUT_SECONDS
    )
    if _is_access_denial(payload) and _has_exact_access_request(payload) and access_approved is not None:
        # Approval is an external fact supplied by the host integration.  It is
        # never inferred from the denial, and there is no retry loop.
        if access_approved() is True:
            completed, payload = _run_json(
                enter_command(event), runner, ENTER_TIMEOUT_SECONDS
            )
    return completed, payload


def process_event(
    event: HostEvent,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    access_approved: Callable[[], bool] | None = None,
) -> dict[str, object]:
    try:
        completed, payload = _enter(event, runner, access_approved)
    except subprocess.TimeoutExpired:
        return warning("adapter_timeout", event.event_name)
    except (OSError, HostEventError):
        return warning("adapter_cli_unavailable", event.event_name)

    if _is_access_denial(payload):
        return _access_output(event, payload)
    if completed.returncode != 0 or payload.get("ok") is not True:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return warning(
            code if isinstance(code, str) else "adapter_cli_failure",
            event.event_name,
        )

    if event.event_name != "SessionEnd":
        return _success_output(event, payload)

    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("session_close") is not True:
        return {
            "systemMessage": "Loop Memory SessionEnd advisory: close deferred by current capabilities."
        }
    try:
        closed, close_payload = _run_json(
            close_command(event), runner, CLOSE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return warning("adapter_timeout", event.event_name)
    except (OSError, HostEventError):
        return warning("adapter_cli_unavailable", event.event_name)
    if closed.returncode != 0 or close_payload.get("ok") is not True:
        return {
            "systemMessage": "Loop Memory SessionEnd advisory: close was not completed."
        }
    return {
        "systemMessage": "Loop Memory SessionEnd advisory: session closed; no promotion was performed."
    }
