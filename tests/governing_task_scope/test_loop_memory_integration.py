import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_scope_guard import base_contract, contract_digest, next_contract


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "skills" / "governing-task-scope"
GUARD = SKILL_ROOT / "scripts" / "scope_guard.py"
LOOP_MEMORY = shutil.which("loop-memory") or str(
    Path.home() / ".local" / "bin" / "loop-memory"
)


class LoopMemoryIntegrationTests(unittest.TestCase):
    def loop(self, *args, expected=0):
        result = subprocess.run(
            [LOOP_MEMORY, *args, "--json"],
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, expected, f"{result.stderr}\n{result.stdout}")
        try:
            body = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"Loop Memory did not return JSON: {result.stdout!r}: {error}")
        self.assertEqual(body.get("ok"), expected == 0, body)
        return body

    def guard(self, directory, event, candidate, current=None):
        candidate_path = directory / f"{event}-candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        command = [
            sys.executable,
            str(GUARD),
            "evaluate",
            "--event",
            event,
            "--candidate",
            str(candidate_path),
            "--json",
        ]
        if current is not None:
            current_path = directory / f"{event}-current.json"
            current_path.write_text(json.dumps(current), encoding="utf-8")
            command.extend(["--current", str(current_path)])
        output_path = directory / f"{event}-approved.json"
        command.extend(["--output", str(output_path)])
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        body = json.loads(result.stdout)
        approved = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else None
        return result, body, approved

    def enter(self, project, root, session_id="host", agent_id=None):
        args = [
            "enter",
            "--cwd",
            str(project),
            "--project-root",
            str(project),
            "--session-id",
            session_id,
            "--root",
            str(root),
        ]
        if agent_id:
            args.extend(["--agent-id", agent_id])
        return self.loop(*args)

    def write(self, project, root, session_id, kind, source, agent_id=None):
        args = [
            "session-write",
            "--cwd",
            str(project),
            "--thread-id",
            session_id,
            "--kind",
            kind,
            "--input",
            str(source),
            "--root",
            str(root),
        ]
        if agent_id:
            args.extend(["--agent-id", agent_id])
        return self.loop(*args)

    def test_contract_round_trip_uses_public_session_status(self):
        with tempfile.TemporaryDirectory(dir=SKILL_ROOT.parent.parent) as raw_dir:
            directory = Path(raw_dir)
            project = directory / "project"
            project.mkdir()
            root = directory / "loop"
            first = self.enter(project, root)

            discovery = base_contract()
            guard_result, guard_body, approved = self.guard(directory, "task-start", discovery)
            self.assertEqual(guard_result.returncode, 0, guard_body)
            self.assertEqual(guard_body["decision"], "allow")
            status_input = directory / "status-discovery.json"
            status_input.write_text(json.dumps(approved), encoding="utf-8")
            written = self.write(project, root, "host", "status", status_input)
            self.assertEqual(written["path"], first["paths"]["status"])

            second = self.enter(project, root)
            self.assertEqual(second["session_id"], first["session_id"])
            self.assertEqual(json.loads(Path(second["paths"]["status"]).read_text()), discovery)

            execution = next_contract(discovery)
            execution["state"] = "execution"
            execution["progress"]["phase"] = "execution"
            result, body, approved = self.guard(
                directory, "execution-contract", execution, current=discovery
            )
            self.assertEqual(result.returncode, 0, body)
            self.assertEqual(body["decision"], "allow")
            execution_input = directory / "status-execution.json"
            execution_input.write_text(json.dumps(approved), encoding="utf-8")
            self.write(project, root, "host", "status", execution_input)

            proposal = next_contract(execution)
            result, body, _ = self.guard(
                directory, "execution-proposal", proposal, current=execution
            )
            self.assertEqual(result.returncode, 0, body)
            self.assertIn(body["decision"], {"allow", "correct"})
            self.assertFalse((root / "superpowers").exists())

    def test_subagent_reentry_rejects_stale_contract_reference(self):
        with tempfile.TemporaryDirectory(dir=SKILL_ROOT.parent.parent) as raw_dir:
            directory = Path(raw_dir)
            project = directory / "project"
            project.mkdir()
            root = directory / "loop"
            main = self.enter(project, root)
            contract = base_contract()
            _, body, approved = self.guard(directory, "task-start", contract)
            self.assertEqual(body["decision"], "allow")
            status_input = directory / "status.json"
            status_input.write_text(json.dumps(approved), encoding="utf-8")
            self.write(project, root, "host", "status", status_input)

            delegation = {
                "contract_ref": {
                    "contract_id": contract["contract_id"],
                    "version": contract["version"],
                    "digest": contract_digest(contract),
                    "milestone": contract["milestone"],
                },
                "work_item_ids": ["W-001"],
            }
            delegation_input = directory / "delegation.json"
            delegation_input.write_text(json.dumps(delegation), encoding="utf-8")
            self.write(project, root, "host", "inbox", delegation_input, agent_id="worker-1")

            worker = self.enter(project, root, agent_id="worker-1")
            received = json.loads(Path(worker["paths"]["agent_inbox"]).read_text())
            self.assertEqual(received, delegation)
            current = json.loads(Path(main["paths"]["status"]).read_text())
            result, body, _ = self.guard(directory, "delegation", received, current=current)
            self.assertEqual(result.returncode, 0, body)
            self.assertEqual(body["decision"], "allow")

            stale = copy.deepcopy(received)
            stale["contract_ref"]["version"] += 1
            result, body, _ = self.guard(directory, "delegation", stale, current=current)
            self.assertEqual(result.returncode, 3, body)
            self.assertEqual(body["decision"], "block")
            self.assertIn("stale_contract_reference", body["reason_codes"])

    def test_round_trip_does_not_require_superpowers(self):
        with tempfile.TemporaryDirectory(dir=SKILL_ROOT.parent.parent) as raw_dir:
            directory = Path(raw_dir)
            project = directory / "project"
            project.mkdir()
            root = directory / "loop"
            self.enter(project, root)
            self.assertNotIn("superpowers", GUARD.read_text(encoding="utf-8").lower())
            skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
            self.assertIn("optional adapter", skill)
            self.assertNotIn("required sub-skill", skill)


if __name__ == "__main__":
    unittest.main()
