import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


RUNTIME = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class AdapterContractTests(unittest.TestCase):
    def load(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_codex_session_start_calls_enter_with_explicit_identity(self):
        from adapters import codex_hook

        event = self.load("codex-session-start.json")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "ok": True, "operation": "enter", "root": "/tmp/loop-memory",
                "project_id": "p-project", "session_id": "s-session",
                "capabilities": {"project_read": True, "session_read": True},
                "notices": [],
                "paths": {"project_memory": "/tmp/loop-memory/projects/p/project.md"},
            }), stderr="",
        )
        with mock.patch.object(codex_hook.subprocess, "run", return_value=completed) as run:
            output = codex_hook.handle(event)

        command = run.call_args.args[0]
        self.assertIn("enter", command)
        self.assertEqual(command[command.index("--cwd") + 1], event["cwd"])
        self.assertEqual(command[command.index("--session-id") + 1], event["session_id"])
        self.assertEqual(command[command.index("--project-root") + 1], event["cwd"])
        self.assertIn("--json", command)
        self.assertIn("hookSpecificOutput", output)
        self.assertNotIn("registry", json.dumps(output))

    def test_codex_subagent_start_passes_agent_id(self):
        from adapters import codex_hook

        event = {
            "session_id": "s1", "cwd": "/tmp/project",
            "hook_event_name": "SubagentStart", "agent_id": "worker-1",
        }
        result = {"ok": True, "operation": "enter", "root": "/tmp/loop-memory",
                  "project_id": "p1", "session_id": "s1", "agent_id": "worker-1",
                  "capabilities": {}, "notices": [], "paths": {}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(result), "")
        with mock.patch.object(codex_hook.subprocess, "run", return_value=completed) as run:
            codex_hook.handle(event)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--agent-id") + 1], "worker-1")

    def test_start_sources_are_supported(self):
        from adapters import codex_hook

        for source in ("startup", "resume", "clear", "compact"):
            event = {"session_id": "s1", "cwd": "/tmp/project",
                     "hook_event_name": "SessionStart", "source": source}
            self.assertEqual(codex_hook.parse_event(event).source, source)

    def test_missing_id_returns_bounded_typed_warning_without_running_cli(self):
        from adapters import codex_hook

        with mock.patch.object(codex_hook.subprocess, "run") as run:
            output = codex_hook.handle({"cwd": "/tmp/project", "hook_event_name": "SessionStart", "source": "startup"})
        run.assert_not_called()
        self.assertFalse(output["ok"])
        self.assertEqual(output["warning"]["code"], "missing_host_identity")
        self.assertLess(len(json.dumps(output)), 1000)

    def test_unknown_version_returns_typed_warning_without_guessing(self):
        from adapters import claude_hook

        event = self.load("claude-session-start.json")
        event["version"] = 99
        with mock.patch.object(claude_hook.subprocess, "run") as run:
            output = claude_hook.handle(event)
        run.assert_not_called()
        self.assertEqual(output["warning"]["code"], "unsupported_event_version")

    def test_missing_end_reason_returns_typed_warning(self):
        from adapters import codex_hook

        event = self.load("codex-session-end.json")
        event.pop("reason")
        with mock.patch.object(codex_hook.subprocess, "run") as run:
            missing = codex_hook.handle(event)
        run.assert_not_called()
        self.assertEqual(missing["warning"]["code"], "invalid_event_reason")

    def test_access_denial_is_model_visible_and_does_not_retry_without_approval(self):
        from adapters import codex_hook

        denied = subprocess.CompletedProcess([], 3, json.dumps({
            "ok": False, "error": {"code": "environment_access_denied",
                                   "message": "access denied", "recoverable": True},
            "required_access": {"path": "~/loop-memory", "read": True, "write": True, "execute": False},
            "next_action": "request_environment_access",
        }), "")
        with mock.patch.object(codex_hook.subprocess, "run", return_value=denied) as run:
            output = codex_hook.handle(self.load("codex-session-start.json"))
        self.assertEqual(run.call_count, 1)
        self.assertIn("systemMessage", output)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertIn("~/loop-memory", json.dumps(output))
        self.assertNotIn("access denied", output.get("systemMessage", ""))

    def test_access_denial_blocks_external_side_effects_until_recovery(self):
        from adapters import codex_hook

        denied = subprocess.CompletedProcess([], 3, json.dumps({
            "ok": False, "error": {"code": "environment_access_denied"},
            "required_access": {"path": "~/loop-memory", "read": True, "write": True, "execute": False},
            "next_action": "request_environment_access",
        }), "")
        with mock.patch.object(codex_hook.subprocess, "run", return_value=denied):
            output = codex_hook.handle(self.load("codex-session-start.json"))

        context = json.loads(output["hookSpecificOutput"]["additionalContext"])
        loop_memory = context["loop_memory"]
        self.assertTrue(loop_memory["blocked"])
        self.assertEqual(
            loop_memory["block_scope"],
            "trusted_state_writes_and_irreversible_external_side_effects",
        )
        self.assertEqual(
            loop_memory["allowed_actions"],
            ["read_only_diagnosis", "recoverable_local_work"],
        )
        self.assertEqual(loop_memory["next_action"], "request_environment_access")

    def test_access_denial_retries_exactly_once_after_approval(self):
        from adapters import codex_hook

        denied = subprocess.CompletedProcess([], 3, json.dumps({
            "ok": False, "error": {"code": "environment_access_denied",
                                   "message": "access denied", "recoverable": True},
            "required_access": {"path": "~/loop-memory", "read": True, "write": True, "execute": False},
        }), "")
        good = subprocess.CompletedProcess([], 0, json.dumps({
            "ok": True, "operation": "enter", "root": "/tmp/loop-memory",
            "project_id": "p1", "session_id": "s1", "capabilities": {},
            "notices": [], "paths": {},
        }), "")
        with mock.patch.object(codex_hook.subprocess, "run", side_effect=[denied, good]) as run:
            output = codex_hook.handle(self.load("codex-session-start.json"), access_approved=lambda: True)
        self.assertEqual(run.call_count, 2)
        self.assertIn("hookSpecificOutput", output)

    def test_access_denial_with_drifted_required_path_stays_generic(self):
        from adapters import codex_hook

        denied = subprocess.CompletedProcess([], 3, json.dumps({
            "ok": False, "error": {"code": "environment_access_denied"},
            "required_access": {"path": "/tmp/secret", "read": True, "write": True, "execute": False},
        }), "")
        with mock.patch.object(codex_hook.subprocess, "run", return_value=denied):
            output = codex_hook.handle(self.load("codex-session-start.json"))
        self.assertIn("no safe access request", output["systemMessage"])
        self.assertNotIn("/tmp/secret", json.dumps(output))

    def test_drifted_access_denial_never_retries_even_if_approved(self):
        from adapters import codex_hook

        denied = subprocess.CompletedProcess([], 3, json.dumps({
            "ok": False, "error": {"code": "environment_access_denied"},
            "required_access": {"path": "/", "read": True, "write": True, "execute": True},
        }), "")
        approved = mock.Mock(return_value=True)
        with mock.patch.object(codex_hook.subprocess, "run", return_value=denied) as run:
            codex_hook.handle(self.load("codex-session-start.json"), access_approved=approved)
        self.assertEqual(run.call_count, 1)
        approved.assert_not_called()

    def test_success_context_contains_metadata_and_selected_paths_only(self):
        from adapters import codex_hook

        result = {"ok": True, "operation": "enter", "root": "/tmp/loop-memory",
                  "project_id": "p1", "session_id": "s1", "capabilities": {"session_read": True, "project_read": True},
                  "notices": [{"code": "notice", "scope": "project", "blocking": []}],
                  "paths": {"project_memory": "/tmp/loop-memory/projects/p1/project.md",
                            "status": "/tmp/loop-memory/status.md",
                            "registry": "/tmp/loop-memory/registry.json"},
                  "memory_body": "SENTINEL-SECRET-TEXT-DO-NOT-LEAK"}
        completed = subprocess.CompletedProcess([], 0, json.dumps(result), "")
        with mock.patch.object(codex_hook.subprocess, "run", return_value=completed):
            output = codex_hook.handle(self.load("codex-session-start.json"))
        rendered = json.dumps(output)
        self.assertIn("p1", rendered)
        self.assertIn("project_memory", rendered)
        self.assertNotIn("SENTINEL-SECRET", rendered)
        self.assertNotIn("registry.json", rendered)

    def test_success_context_strips_untrusted_nested_capability_and_notice_fields(self):
        from adapters import codex_hook

        result = {"ok": True, "operation": "enter", "root": "/tmp/loop-memory",
                  "project_id": "p1", "session_id": "s1",
                  "capabilities": {"session_read": True, "secret": "credential"},
                  "notices": [{"code": "notice", "scope": "project", "blocking": [],
                               "body": "SENTINEL-SECRET-TEXT-DO-NOT-LEAK"}], "paths": {}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(result), "")
        with mock.patch.object(codex_hook.subprocess, "run", return_value=completed):
            output = codex_hook.handle(self.load("codex-session-start.json"))
        rendered = json.dumps(output)
        self.assertIn("session_read", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("SENTINEL-SECRET", rendered)

    def test_session_end_is_advisory_and_closes_only_when_capability_allows(self):
        from adapters import codex_hook

        entered = {"ok": True, "operation": "enter", "root": "/tmp/loop-memory",
                   "project_id": "p1", "session_id": "s1",
                   "capabilities": {"session_close": True}, "notices": [],
                   "paths": {"agent_outbox": "/tmp/loop-memory/outbox.md"}}
        closed = {"ok": True, "operation": "session-close", "root": "/tmp/loop-memory",
                  "project_id": "p1", "session_id": "s1", "path": "/tmp/archive/s1"}
        responses = [subprocess.CompletedProcess([], 0, json.dumps(entered), ""),
                     subprocess.CompletedProcess([], 0, json.dumps(closed), "")]
        with mock.patch.object(codex_hook.subprocess, "run", side_effect=responses) as run:
            output = codex_hook.handle(self.load("codex-session-end.json"))
        self.assertEqual(run.call_count, 2)
        self.assertIn("advisory", json.dumps(output))
        self.assertNotIn("transcript", json.dumps(output))

    def test_session_end_does_not_close_when_capability_denied(self):
        from adapters import codex_hook

        entered = {"ok": True, "operation": "enter", "root": "/tmp/loop-memory",
                   "project_id": "p1", "session_id": "s1",
                   "capabilities": {"session_close": False}, "notices": [],
                   "paths": {"agent_outbox": "/tmp/loop-memory/outbox.md"}}
        with mock.patch.object(codex_hook.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(entered), "")) as run:
            output = codex_hook.handle(self.load("codex-session-end.json"))
        self.assertEqual(run.call_count, 1)
        self.assertIn("close", json.dumps(output))

    def test_timeout_is_bounded_advisory(self):
        from adapters import claude_hook

        with mock.patch.object(claude_hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("loop-memory", 2.0)):
            output = claude_hook.handle(self.load("claude-session-start.json"))
        self.assertEqual(output["warning"]["code"], "adapter_timeout")
        self.assertLess(len(json.dumps(output)), 1000)


if __name__ == "__main__":
    unittest.main()
