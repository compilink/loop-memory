import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import migration as migration_module
from scripts.loopmem.registry import RegistryStore


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop_memory.py"
SECRET = "SENTINEL-SECRET-TEXT-DO-NOT-LEAK"


class CompatibilitySkillContractTests(unittest.TestCase):
    def test_skill_documents_scoped_hot_path_and_complete_lifecycle(self):
        skill_root = Path(__file__).resolve().parents[1]
        main = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        operations = (skill_root / "references/operations.md").read_text(
            encoding="utf-8"
        )

        for required in (
            "`doctor`",
            "`session-close`",
            "live state",
            "compaction, transfer, or close",
            "no durable learning or resumable state changed",
        ):
            self.assertIn(required, main)
        for required in (
            "External legacy sources are read-only",
            "matching next action",
            "not a periodic job",
            "deprecated for new writes",
            "other-project",
        ):
            self.assertIn(required, operations)

    def test_main_skill_keeps_semantics_in_agent_and_mechanics_in_cli(self):
        skill_root = Path(__file__).resolve().parents[1]
        body = (skill_root / "SKILL.md").read_text(encoding="utf-8")

        self.assertLessEqual(len(body.split()), 500)
        for required in (
            "External legacy sources are read-only",
            "candidate",
            "Read [references/operations.md]",
        ):
            self.assertIn(required, body)
        for mechanical_detail in ("os.rename", "shutil.rmtree", "receipt.json"):
            self.assertNotIn(mechanical_detail, body)

    def test_runtime_skill_keeps_compatibility_forward_only(self):
        """The installed runtime must not revive legacy command vocabulary."""
        runtime_root = Path(__file__).resolve().parents[1]
        body = (runtime_root / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("managing-loop-memory", body)
        self.assertIn("External legacy sources are read-only", body)
        self.assertNotIn("`write-session`", body)
        self.assertNotIn("`migrate-legacy`", body)
        self.assertNotIn("~/.codex/loop-memory", body)

class CliTestCase(unittest.TestCase):
    def run_cli(
        self,
        *arguments: object,
        process_cwd: Path | None = None,
        home: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if home is not None:
            environment["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(str(value) for value in arguments)],
            capture_output=True,
            check=False,
            cwd=process_cwd,
            env=environment,
            text=True,
            timeout=10,
        )

    def json_payload(
        self,
        completed: subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        payload = json.loads(completed.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def test_project_horizon_promotion_uses_explicit_file_without_creating_siblings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            entered = self.assert_success(
                self.run_cli(
                    "enter", "--cwd", project, "--session-id", "host",
                    "--root", root, "--json", home=home,
                ), "enter", root,
            )
            self.assertIn("project_long", entered["paths"])
            self.assertIn("project_medium", entered["paths"])
            self.assertIn("project_short", entered["paths"])
            self.assertFalse(Path(entered["paths"]["project_short"]).exists())
            entry = home / "short.md"
            entry.write_text(
                "- [2026-08-31][verified] Current task summary.\n"
                "  Evidence: focused test\n",
                encoding="utf-8",
            )
            promoted = self.assert_success(
                self.run_cli(
                    "promote", "--cwd", project, "--thread-id", "host",
                    "--scope", "project-short", "--section", "Entries",
                    "--input", entry, "--root", root, "--json", home=home,
                ), "promote", root,
            )
            self.assertTrue(promoted["changed"])
            self.assertIn("Current task summary", Path(entered["paths"]["project_short"]).read_text())
            self.assertFalse(Path(entered["paths"]["project_medium"]).exists())

    def test_global_fact_promotion_returns_index_and_detail_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            entered = self.assert_success(
                self.run_cli(
                    "enter", "--cwd", project, "--session-id", "host",
                    "--root", root, "--json", home=home,
                ), "enter", root,
            )
            self.assertTrue(entered["ok"])
            entry = home / "fact.md"
            entry.write_text(
                "- [2026-08-14][verified] A canonical global fact.\n"
                "  Evidence: CLI contract test\n",
                encoding="utf-8",
            )

            promoted = self.assert_success(
                self.run_cli(
                    "promote", "--cwd", project, "--thread-id", "host",
                    "--scope", "global-fact", "--section", "Entries",
                    "--input", entry, "--root", root, "--json", home=home,
                ), "promote", root,
            )

            self.assertTrue(promoted["ok"])
            self.assertIn("global_fact_index", promoted["paths"])
            self.assertIn("global_facts", promoted["paths"])
            self.assertTrue(Path(promoted["paths"]["global_fact_index"]).is_file())

    def test_legacy_long_enter_returns_nonblocking_organization_notice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.assert_success(
                self.run_cli(
                    "enter", "--cwd", project, "--session-id", "host",
                    "--root", root, "--json", home=home,
                ), "enter", root,
            )
            self.assertTrue(first["ok"])
            Path(first["paths"]["global_long"]).write_text(
                "# Global Long-Term Memory\n\n## Entries\n\n"
                "- [2026-08-14][verified] Legacy.\n"
                "  Evidence: fixture\n",
                encoding="utf-8",
            )

            second = self.assert_success(
                self.run_cli(
                    "enter", "--cwd", project, "--session-id", "host",
                    "--root", root, "--json", home=home,
                ), "enter", root,
            )

            self.assertTrue(second["ok"])
            self.assertTrue(second["capabilities"]["global_read"])
            self.assertIn(
                "global_long_organization_due",
                {notice["code"] for notice in second["notices"]},
            )

    def test_global_organize_replaces_legacy_long_and_returns_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.assert_success(
                self.run_cli(
                    "enter", "--cwd", project, "--session-id", "host",
                    "--root", root, "--json", home=home,
                ), "enter", root,
            )
            legacy = (
                "# Global Long-Term Memory\n\n## Entries\n\n"
                "- [2026-08-14][verified] Legacy.\n"
                "  Evidence: fixture\n"
            )
            Path(first["paths"]["global_long"]).write_text(legacy, encoding="utf-8")
            methodology = home / "methodology.md"
            methodology.write_text(
                "# Global Long-Term Memory\n\n## Methodology\n\n"
                "- [2026-08-14][verified] Keep the context concise.\n"
                "  Evidence: CLI contract test\n\n## Fact Index\n\n"
                "- `~/loop-memory/global/facts/index.md`\n",
                encoding="utf-8",
            )

            organized = self.assert_success(
                self.run_cli(
                    "global-organize", "--cwd", project, "--thread-id", "host",
                    "--methodology", methodology, "--root", root, "--json",
                    home=home,
                ), "global-organize", root,
            )

            self.assertTrue(organized["ok"])
            self.assertTrue(organized["changed"])
            self.assertTrue(Path(organized["history"]).is_file())
            self.assertTrue(Path(organized["receipt"]).is_file())

    def assert_success(
        self,
        completed: subprocess.CompletedProcess[str],
        operation: str,
        root: Path,
    ) -> dict[str, object]:
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        payload = self.json_payload(completed)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["operation"], operation)
        self.assertEqual(payload["root"], str(root.resolve()))
        self.assertIsInstance(payload["warnings"], list)
        return payload

    def assert_failure(
        self,
        completed: subprocess.CompletedProcess[str],
        exit_code: int,
        error_code: str,
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, exit_code, completed.stderr)
        payload = self.json_payload(completed)
        self.assertEqual(set(payload), {"ok", "error"})
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], error_code)
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertIsInstance(payload["error"]["recoverable"], bool)
        return payload


class PreflightSmokeTests(CliTestCase):
    def test_access_check_returns_one_success_object_and_creates_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"

            completed = self.run_cli(
                "access-check",
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "access-check", root)
            self.assertTrue(root.is_dir())
            self.assertNotIn("required_access", payload)

    def test_access_check_denial_is_actionable_and_body_free(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop-memory"
            root.mkdir()
            fixture = """
import importlib.util
from pathlib import Path
import sys
from unittest import mock

script = Path(sys.argv[1])
specification = importlib.util.spec_from_file_location(
    "loop_memory_access_subprocess_contract",
    script,
)
module = importlib.util.module_from_spec(specification)
assert specification.loader is not None
specification.loader.exec_module(module)
with mock.patch.object(
    module.access.os,
    "replace",
    side_effect=PermissionError("SENTINEL-SECRET-TEXT-DO-NOT-LEAK"),
):
    raise SystemExit(
        module.main(
            [
                "access-check",
                "--root",
                sys.argv[2],
                "--json",
            ]
        )
)
"""
            completed = subprocess.run(
                [sys.executable, "-c", fixture, str(SCRIPT), str(root)],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )

            payload = self.json_payload(completed)
            self.assertEqual(completed.returncode, 3)
            self.assertIs(payload["ok"], False)
            self.assertEqual(
                payload["error"]["code"],
                "environment_access_denied",
            )
            self.assertEqual(
                payload["next_action"],
                "request_environment_access",
            )
            self.assertEqual(completed.stdout.count('"required_access"'), 1)
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn("internal_error", completed.stdout)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(list(root.iterdir()), [])

    def test_preflight_returns_identity_json(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir).resolve()
            cwd = root / "project"
            cwd.mkdir()

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-1",
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "preflight", root)
            self.assertIn("project_id", payload)
            self.assertIn("session_id", payload)

    def test_preflight_runs_from_unrelated_cwd_and_returns_contained_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            unrelated = temp / "unrelated"
            cwd.mkdir()
            unrelated.mkdir()

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-elsewhere",
                "--agent-id",
                "worker",
                "--root",
                root,
                "--json",
                process_cwd=unrelated,
            )

            payload = self.assert_success(completed, "preflight", root)
            self.assertTrue(payload["project_id"].startswith("p-"))
            self.assertTrue(payload["session_id"].startswith("s-"))
            self.assertEqual(payload["agent_id"], "worker")
            for path in payload["paths"].values():
                Path(path).relative_to(root)
            self.assertIn(
                "/agents/subagents/worker/",
                payload["paths"]["agent_outbox"],
            )
            self.assertTrue(root.is_dir())
            self.assertTrue((root / "registry.json").is_file())

    def test_project_and_thread_identity_are_stable_across_initialize_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            arguments = (
                "initialize",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-stable",
                "--root",
                root,
                "--json",
            )

            first = self.assert_success(
                self.run_cli(*arguments),
                "initialize",
                root,
            )
            second = self.assert_success(
                self.run_cli(*arguments),
                "initialize",
                root,
            )

            self.assertEqual(first["project_id"], second["project_id"])
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertEqual(first["paths"], second["paths"])
            for name in (
                "global_long",
                "global_medium",
                "global_short",
                "project_memory",
            ):
                self.assertTrue(Path(first["paths"][name]).is_file())
            for name in ("status", "handoff", "agent_inbox", "agent_outbox"):
                self.assertFalse(Path(first["paths"][name]).exists())


class CommandContractTests(CliTestCase):
    def test_session_close_archives_idempotently_and_blocks_unresolved_outbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            identity = self.assert_success(
                self.run_cli(
                    "preflight",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-close",
                    "--root",
                    root,
                    "--json",
                ),
                "preflight",
                root,
            )
            active = Path(identity["paths"]["session"])
            outbox_input = temp / "outbox.md"
            outbox_input.write_text(
                "# Main Agent Outbox\n\n- unresolved candidate\n",
                encoding="utf-8",
            )
            self.assert_success(
                self.run_cli(
                    "session-write",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-close",
                    "--kind",
                    "outbox",
                    "--input",
                    outbox_input,
                    "--root",
                    root,
                    "--json",
                ),
                "session-write",
                root,
            )

            blocked = self.run_cli(
                "session-close",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-close",
                "--root",
                root,
                "--json",
            )

            self.assert_failure(blocked, 3, "capability_denied")
            self.assertTrue(active.is_dir())

            outbox_input.write_text("# Main Agent Outbox\n", encoding="utf-8")
            self.assert_success(
                self.run_cli(
                    "session-write",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-close",
                    "--kind",
                    "outbox",
                    "--input",
                    outbox_input,
                    "--root",
                    root,
                    "--json",
                ),
                "session-write",
                root,
            )
            closed = self.assert_success(
                self.run_cli(
                    "session-close",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-close",
                    "--root",
                    root,
                    "--json",
                ),
                "session-close",
                root,
            )
            archived = Path(closed["path"])
            self.assertFalse(active.exists())
            self.assertTrue(archived.is_dir())

            repeated = self.assert_success(
                self.run_cli(
                    "session-close",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-close",
                    "--root",
                    root,
                    "--json",
                ),
                "session-close",
                root,
            )
            self.assertEqual(repeated["path"], str(archived))

    def test_doctor_reports_actionable_body_free_migration_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            legacy_project = temp / "legacy-project"
            legacy = legacy_project / ".memory"
            legacy.mkdir(parents=True)
            (legacy / "project.md").write_text(
                f"# Legacy\n\n{SECRET}\n",
                encoding="utf-8",
            )
            current_project = temp / "current-project"
            current_project.mkdir()
            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    legacy_project,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-scan",
                root,
            )
            manifest_path = Path(scanned["manifests"][0])

            completed = self.run_cli(
                "doctor",
                "--cwd",
                current_project,
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "doctor", root)
            self.assertEqual(len(payload["incomplete_migrations"]), 1)
            record = payload["incomplete_migrations"][0]
            self.assertEqual(record["manifest"], str(manifest_path))
            self.assertEqual(record["source_kind"], "project")
            self.assertEqual(record["blocking_scope"], "other-project")
            self.assertIs(record["protected"], False)
            self.assertEqual(
                record["next_action"],
                {
                    "operation": "migrate-apply",
                    "requires_classification": True,
                    "requires_explicit_approval": False,
                },
            )
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)

    def test_permission_error_is_typed_environment_access_denied(self):
        specification = importlib.util.spec_from_file_location(
            "loop_memory_permission_contract",
            SCRIPT,
        )
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        specification.loader.exec_module(module)
        output = io.StringIO()

        with mock.patch.object(
            module,
            "_dispatch",
            side_effect=PermissionError("sensitive path"),
        ), mock.patch.object(module.sys, "stdout", output):
            exit_code = module.main(
                [
                    "preflight",
                    "--cwd",
                    "/tmp",
                    "--json",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertEqual(
            set(payload),
            {"ok", "error", "required_access", "next_action"},
        )
        self.assertEqual(
            set(payload["error"]),
            {"code", "message", "recoverable"},
        )
        self.assertEqual(payload["error"]["code"], "environment_access_denied")
        self.assertIs(payload["error"]["recoverable"], True)
        self.assertEqual(
            payload["required_access"],
            {
                "path": "~/loop-memory",
                "read": True,
                "write": True,
                "execute": False,
            },
        )
        self.assertEqual(payload["next_action"], "request_environment_access")
        self.assertEqual(output.getvalue().count('"required_access"'), 1)
        self.assertNotIn("internal_error", output.getvalue())
        self.assertNotIn("sensitive path", output.getvalue())

    def test_json_help_is_one_object_for_top_level_and_subcommands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve() / "home"
            expected_root = home / "loop-memory"
            cases = (
                (("--json", "--help"), None),
                (("preflight", "--help", "--json"), "preflight"),
                (("preflight", "--json", "--help"), "preflight"),
                (("legacy-stage", "--help", "--json"), "legacy-stage"),
                (("legacy-delete", "--help", "--json"), "legacy-delete"),
                (("migrate-refresh", "--help", "--json"), "migrate-refresh"),
                (("migrate-refresh", "--json", "--help"), "migrate-refresh"),
            )

            for arguments, command in cases:
                with self.subTest(arguments=arguments):
                    completed = self.run_cli(*arguments, home=home)
                    payload = self.assert_success(completed, "help", expected_root)
                    self.assertEqual(payload["command"], command)
                    self.assertEqual(completed.stderr, "")
                    self.assertFalse(expected_root.exists())

    def test_migrate_refresh_requires_only_manifest_root_and_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            manifest = temp / "manifest.json"
            cases = (
                ("--cwd", SECRET),
                ("--classification", SECRET),
                ("--legacy-path", SECRET),
                (SECRET,),
            )

            for unrelated in cases:
                with self.subTest(unrelated=unrelated):
                    completed = self.run_cli(
                        "migrate-refresh",
                        "--manifest",
                        manifest,
                        *unrelated,
                        "--root",
                        root,
                        "--json",
                    )
                    self.assert_failure(completed, 2, "usage")
                    self.assertNotIn(SECRET, completed.stdout)
                    self.assertNotIn(SECRET, completed.stderr)
                    self.assertFalse(root.exists())

            missing_manifest = self.run_cli(
                "migrate-refresh",
                "--root",
                root,
                "--json",
            )
            self.assert_failure(missing_manifest, 2, "usage")
            self.assertFalse(root.exists())

            missing_json = self.run_cli(
                "migrate-refresh",
                "--manifest",
                manifest,
                "--root",
                root,
            )
            self.assertEqual(missing_json.returncode, 2)
            self.assertEqual(missing_json.stdout, "")
            self.assertIn("usage", missing_json.stderr)
            self.assertFalse(root.exists())

    def test_legacy_stage_and_delete_emit_bounded_body_free_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text(SECRET, encoding="utf-8")

            staged = self.assert_success(
                self.run_cli(
                    "legacy-stage", "--cwd", cwd, "--root", root, "--json"
                ),
                "legacy-stage",
                root,
            )
            observable = json.dumps(staged, sort_keys=True)
            self.assertNotIn(SECRET, observable)
            self.assertNotIn("legacy.md", observable)
            self.assertTrue(source.is_dir())

            deleted = self.assert_success(
                self.run_cli(
                    "legacy-delete",
                    "--snapshot",
                    staged["snapshot_id"],
                    "--root",
                    root,
                    "--json",
                ),
                "legacy-delete",
                root,
            )
            self.assertIs(deleted["deleted"], True)
            self.assertTrue(Path(deleted["receipt_path"]).is_file())
            self.assertFalse(Path(deleted["snapshot_path"]).exists())

    def test_legacy_stage_respects_existing_manifest_phase_and_inventory(self):
        for case, expected in (
            ("copied", "migration_required"),
            ("drift", "migration_refresh_required"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                root = temp / "loop"
                cwd = temp / "project"
                source = cwd / ".memory"
                source.mkdir(parents=True)
                (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
                manifest_path = Path(
                    migration_module.scan_legacy(root, cwd, [])["manifests"][0]
                )
                snapshots_before = sorted((root / "legacy-snapshots").iterdir())
                if case == "copied":
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest.update(
                        {
                            "state": "copied",
                            "classification_sha256": "a" * 64,
                            "target_files": [],
                            "staging_path": str(
                                root
                                / "migrations/staging"
                                / manifest["migration_id"]
                            ),
                            "publish_plan_sha256": "b" * 64,
                        }
                    )
                    migration_module.write_json_atomic(
                        manifest_path,
                        migration_module._manifest_storage_value(manifest, root),
                    )
                else:
                    (source / "legacy.md").write_text("changed\n", encoding="utf-8")

                completed = self.run_cli(
                    "legacy-stage", "--cwd", cwd, "--root", root, "--json"
                )

                self.assert_failure(completed, 3, expected)
                self.assertTrue(source.is_dir())
                self.assertEqual(
                    sorted((root / "legacy-snapshots").iterdir()),
                    snapshots_before,
                )

    def test_project_legacy_preflight_is_typed_without_creating_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text(SECRET, encoding="utf-8")

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-legacy",
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "preflight", root)
            self.assertIs(payload["degraded"], True)
            self.assertFalse(payload["capabilities"]["project_promote"])
            self.assertTrue(payload["capabilities"]["session_write"])
            self.assertTrue(source.is_dir())
            self.assertFalse((root / "migrations/manifests").exists())
            self.assertNotIn(SECRET, completed.stdout + completed.stderr)

    def test_project_legacy_non_directory_is_fail_closed(self):
        cases = ("file", "fifo")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                root = temp / "loop"
                cwd = temp / "project"
                cwd.mkdir()
                source = cwd / ".memory"
                if case == "file":
                    source.write_text(SECRET, encoding="utf-8")
                else:
                    os.mkfifo(source)
                completed = self.run_cli(
                    "preflight", "--cwd", cwd, "--root", root, "--json"
                )
                self.assert_failure(completed, 4, "unsafe_legacy_source")
                self.assertFalse((root / "migrations/manifests").exists())

    def test_json_help_rejects_unknown_commands_and_invalid_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve() / "home"
            cases = (
                ("garbage", "preflight", "--help", "--json"),
                ("preflight", "garbage", "--help", "--json"),
                ("garbage", "--help", "--json"),
            )

            for arguments in cases:
                with self.subTest(arguments=arguments):
                    completed = self.run_cli(*arguments, home=home)
                    self.assert_failure(completed, 2, "usage")
                    self.assertEqual(completed.stderr, "")
                    self.assertFalse((home / "loop-memory").exists())

    def test_credential_source_risk_never_leaks_through_json_or_stderr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            secret = f"{SECRET}-CREDENTIAL"
            (source / "project.md").write_text(
                f"SERVICE_TOKEN={secret}\n",
                encoding="utf-8",
            )

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-secret-remote",
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "preflight", root)
            self.assertIs(payload["degraded"], True)
            self.assertFalse(payload["capabilities"]["project_read"])
            self.assertFalse(payload["capabilities"]["project_promote"])
            self.assertFalse(payload["capabilities"]["migration_apply"])
            self.assertTrue(payload["capabilities"]["session_write"])
            self.assertTrue(payload["capabilities"]["global_promote"])
            observable = json.dumps(payload, sort_keys=True) + completed.stderr
            self.assertNotIn(secret, observable)

    def test_published_commands_dispatch_to_core_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            legacy = temp / "legacy" / ".memory"
            cwd.mkdir()
            legacy.mkdir(parents=True)
            status_input = temp / "status.md"
            status_input.write_text("# Session Status\n\nphase: active\n", encoding="utf-8")
            entry_input = temp / "entry.md"
            entry_input.write_text(
                "- [2026-08-10][verified] CLI dispatch reaches promotion.\n"
                "  Evidence: tests/test_cli.py\n",
                encoding="utf-8",
            )
            identity_arguments = (
                "--cwd",
                cwd,
                "--thread-id",
                "thread-dispatch",
                "--root",
                root,
                "--json",
            )

            initialized = self.assert_success(
                self.run_cli("initialize", *identity_arguments),
                "initialize",
                root,
            )
            written = self.assert_success(
                self.run_cli(
                    "session-write",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-dispatch",
                    "--kind",
                    "status",
                    "--input",
                    status_input,
                    "--root",
                    root,
                    "--json",
                ),
                "session-write",
                root,
            )
            promoted = self.assert_success(
                self.run_cli(
                    "promote",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-dispatch",
                    "--scope",
                    "project",
                    "--section",
                    "Verified Facts",
                    "--input",
                    entry_input,
                    "--root",
                    root,
                    "--json",
                ),
                "promote",
                root,
            )
            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    cwd,
                    "--legacy-path",
                    legacy,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-scan",
                root,
            )
            manifest_path = Path(scanned["manifests"][0])
            refreshed = self.assert_success(
                self.run_cli(
                    "migrate-refresh",
                    "--manifest",
                    manifest_path,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-refresh",
                root,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            classification = temp / "classification.json"
            classification.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": ".",
                                "destination": "discard_empty",
                                "mode": "discard_empty",
                            }
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )
            applied = self.assert_success(
                self.run_cli(
                    "migrate-apply",
                    "--manifest",
                    manifest_path,
                    "--classification",
                    classification,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-apply",
                root,
            )
            maintained = self.assert_success(
                self.run_cli("maintain", "--root", root, "--json"),
                "maintain",
                root,
            )
            diagnosed = self.assert_success(
                self.run_cli(
                    "diagnose",
                    "--cwd",
                    cwd,
                    "--root",
                    root,
                    "--json",
                ),
                "diagnose",
                root,
            )

            self.assertEqual(
                Path(written["path"]).read_text(encoding="utf-8"),
                status_input.read_text(encoding="utf-8"),
            )
            self.assertIs(promoted["changed"], True)
            self.assertIn(
                "CLI dispatch reaches promotion.",
                Path(initialized["paths"]["project_memory"]).read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(refreshed["migration"]["state"], "inventoried")
            self.assertEqual(
                refreshed["migration"]["previous_inventory_sha256"],
                refreshed["migration"]["current_inventory_sha256"],
            )
            self.assertEqual(applied["migration"]["state"], "complete")
            self.assertIn("deleted", maintained)
            self.assertIn("stale_locks", diagnosed)
            self.assertIn("root_metadata", diagnosed)

    def test_json_failures_use_stable_usage_blocked_and_corrupt_exit_codes(self):
        usage = self.run_cli("preflight", "--json")
        self.assert_failure(usage, 2, "usage")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            body = temp / "body.md"
            body.write_text("# Session Status\n", encoding="utf-8")
            blocked = self.run_cli(
                "session-write",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-errors",
                "--kind",
                "status",
                "--agent-id",
                "worker",
                "--input",
                body,
                "--root",
                root,
                "--json",
            )
            blocked_payload = self.assert_failure(blocked, 3, "invalid_agent_scope")
            self.assertIs(blocked_payload["error"]["recoverable"], True)

            corrupt_root = temp / "corrupt-loop"
            corrupt_root.mkdir()
            (corrupt_root / "registry.json").write_text(
                "not-json",
                encoding="utf-8",
            )
            corrupt = self.run_cli(
                "initialize",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-corrupt",
                "--root",
                corrupt_root,
                "--json",
            )
            corrupt_payload = self.assert_failure(corrupt, 4, "corrupt_state")
            self.assertIs(corrupt_payload["error"]["recoverable"], False)
            self.assertFalse((corrupt_root / "locks").exists())

    def test_session_and_promotion_bodies_are_file_only_and_never_echoed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            body = temp / "body.md"
            body.write_text(f"# Session Status\n\n{SECRET}\n", encoding="utf-8")

            rejected = self.run_cli(
                "session-write",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-body",
                "--kind",
                "status",
                "--body",
                SECRET,
                "--root",
                root,
                "--json",
            )
            self.assert_failure(rejected, 2, "usage")
            self.assertNotIn(SECRET, rejected.stdout)
            self.assertNotIn(SECRET, rejected.stderr)

            written = self.assert_success(
                self.run_cli(
                    "session-write",
                    "--cwd",
                    cwd,
                    "--thread-id",
                    "thread-body",
                    "--kind",
                    "status",
                    "--input",
                    body,
                    "--root",
                    root,
                    "--json",
                ),
                "session-write",
                root,
            )
            self.assertNotIn(SECRET, written["path"])
            self.assertNotIn(SECRET, rejected.stderr)
            self.assertNotIn(SECRET, json.dumps(written))
            self.assertIn(SECRET, Path(written["path"]).read_text(encoding="utf-8"))

    def test_input_files_must_be_regular_nonsymlink_utf8_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            target = temp / "target.md"
            target.write_text(SECRET, encoding="utf-8")
            symlink = temp / "symlink.md"
            symlink.symlink_to(target)
            invalid_utf8 = temp / "invalid.md"
            invalid_utf8.write_bytes(b"\xff\xfe")

            for source in (symlink, invalid_utf8, temp):
                with self.subTest(source=source):
                    completed = self.run_cli(
                        "session-write",
                        "--cwd",
                        cwd,
                        "--thread-id",
                        "thread-input",
                        "--kind",
                        "status",
                        "--input",
                        source,
                        "--root",
                        root,
                        "--json",
                    )
                    self.assert_failure(completed, 3, "invalid_input_file")
                    self.assertNotIn(SECRET, completed.stdout)
                    self.assertNotIn(SECRET, completed.stderr)

    def test_migrate_refresh_manifest_validation_never_echoes_metadata_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            target = temp / "manifest.json"
            target.write_text(SECRET, encoding="utf-8")
            symlink = temp / "manifest-link.json"
            symlink.symlink_to(target)

            completed = self.run_cli(
                "migrate-refresh",
                "--manifest",
                symlink,
                "--root",
                root,
                "--json",
            )

            self.assert_failure(completed, 3, "invalid_manifest_file")
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)


class MigrationPreflightTests(CliTestCase):
    def test_unrelated_inventoried_project_migration_does_not_block_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            legacy_project = temp / "legacy-project"
            legacy = legacy_project / ".memory"
            legacy.mkdir(parents=True)
            (legacy / "project.md").write_text(
                "# Legacy Project Memory\n",
                encoding="utf-8",
            )
            current_project = temp / "current-project"
            current_project.mkdir()

            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    legacy_project,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-scan",
                root,
            )
            manifest_path = Path(scanned["manifests"][0])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "inventoried")
            self.assertEqual(manifest["source_kind"], "project")

            completed = self.run_cli(
                "preflight",
                "--cwd",
                current_project,
                "--thread-id",
                "thread-current",
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "preflight", root)
            self.assertNotEqual(payload["project_id"], manifest["project_id"])

    def test_staged_project_legacy_releases_only_matching_inventoried_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
            scanned = migration_module.scan_legacy(root, cwd, [])
            manifest_path = Path(scanned["manifests"][0])
            manifest_before = migration_module.load_manifest(manifest_path)

            self.assert_success(
                self.run_cli(
                    "legacy-stage", "--cwd", cwd, "--root", root, "--json"
                ),
                "legacy-stage",
                root,
            )
            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-staged",
                "--root",
                root,
                "--json",
            )

            self.assert_success(completed, "preflight", root)
            manifest_after = migration_module.load_manifest(manifest_path)
            self.assertEqual(manifest_after["migration_id"], manifest_before["migration_id"])
            self.assertEqual(manifest_after["state"], manifest_before["state"])
            self.assertEqual(manifest_after["source"], manifest_before["source"])
            self.assertEqual(manifest_after["source_inventory_sha256"], manifest_before["source_inventory_sha256"])

    def test_staged_project_legacy_does_not_release_copied_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
            manifest_path = Path(migration_module.scan_legacy(root, cwd, [])["manifests"][0])
            manifest = migration_module.load_manifest(manifest_path)
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_staged_gate_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            from scripts.loopmem import legacy as legacy_module

            legacy_module.stage_legacy(root, cwd)
            manifest["state"] = "copied"
            with mock.patch.object(
                module.migration,
                "load_manifest",
                return_value=manifest,
            ), mock.patch.object(
                module.migration,
                "recover_migration",
                side_effect=LoopMemoryError(
                    code="source_changed",
                    message="copied migration remains blocking",
                    recoverable=False,
                ),
            ):
                with self.assertRaises(LoopMemoryError) as context:
                    module._check_pending_migrations(root)

            self.assertEqual(context.exception.code, "source_changed")

    def test_staged_receipt_does_not_hide_invalid_migration_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
            migration_module.scan_legacy(root, cwd, [])
            from scripts.loopmem import legacy as legacy_module

            legacy_module.stage_legacy(root, cwd)
            (root / "migrations/ledger.jsonl").write_text(
                '{"invalid":true}\n',
                encoding="utf-8",
            )
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_staged_ledger_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)

            with self.assertRaises(LoopMemoryError) as context:
                module._check_pending_migrations(root)

            self.assertEqual(context.exception.code, "corrupt_state")

    def test_staged_receipt_does_not_hide_invalid_manifest_location(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
            manifest_path = Path(
                migration_module.scan_legacy(root, cwd, [])["manifests"][0]
            )
            from scripts.loopmem import legacy as legacy_module

            legacy_module.stage_legacy(root, cwd)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["target"] = "../outside"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_staged_manifest_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)

            with self.assertRaises(LoopMemoryError) as context:
                module._check_pending_migrations(root)

            self.assertEqual(context.exception.code, "corrupt_state")

    def test_migrate_refresh_validates_namespace_while_migration_lease_is_held(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = temp / "legacy" / ".memory"
            cwd.mkdir()
            source.mkdir(parents=True)
            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    cwd,
                    "--legacy-path",
                    source,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-scan",
                root,
            )
            manifest_path = Path(scanned["manifests"][0])
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_refresh_lease_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            original_validate = module._validate_migration_namespace
            observations = []

            def validate_under_lease(selected_root, selected_path, *, recover=True):
                observations.append(
                    (
                        (selected_root / "locks" / "migration.lock").is_file(),
                        recover,
                    )
                )
                return original_validate(
                    selected_root,
                    selected_path,
                    recover=recover,
                )

            arguments = module._parser().parse_args(
                [
                    "migrate-refresh",
                    "--manifest",
                    str(manifest_path),
                    "--root",
                    str(root),
                    "--json",
                ]
            )
            with mock.patch.object(
                module,
                "_validate_migration_namespace",
                side_effect=validate_under_lease,
            ):
                result = module._dispatch(arguments)

            self.assertEqual(result["operation"], "migrate-refresh")
            self.assertEqual(observations, [(True, False)])

    def test_source_unstable_from_recovery_stays_recoverable_and_typed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = temp / "legacy" / ".memory"
            cwd.mkdir()
            source.mkdir(parents=True)
            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    cwd,
                    "--legacy-path",
                    source,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-scan",
                root,
            )
            self.assertEqual(len(scanned["manifests"]), 1)
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_unstable_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            injected = LoopMemoryError(
                code="source_unstable",
                message="Legacy source changed or could not be read consistently",
                recoverable=True,
            )

            with mock.patch.object(
                module.migration,
                "recover_migration",
                side_effect=injected,
            ), mock.patch.object(
                module.legacy,
                "has_staged_receipt",
                return_value=False,
            ):
                with self.assertRaises(LoopMemoryError) as context:
                    module._check_pending_migrations(root)

            self.assertEqual(context.exception.code, "source_unstable")
            self.assertIs(context.exception.recoverable, True)
            self.assertEqual(module._exit_for_error(context.exception), 3)
            self.assertEqual(
                module._failure(context.exception),
                {
                    "ok": False,
                    "error": {
                        "code": "source_unstable",
                        "message": (
                            "Legacy source changed or could not be read consistently"
                        ),
                        "recoverable": True,
                    },
                },
            )

    def test_later_transition_metadata_prevents_refresh_required_translation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            source = temp / "legacy" / ".memory"
            cwd.mkdir()
            source.mkdir(parents=True)
            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    cwd,
                    "--legacy-path",
                    source,
                    "--root",
                    root,
                    "--json",
                ),
                "migrate-scan",
                root,
            )
            manifest_path = Path(scanned["manifests"][0])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["target_files"] = None
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_refresh_eligibility_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            injected = LoopMemoryError(
                code="source_changed",
                message="Legacy source changed after migration inventory",
                recoverable=False,
            )

            with mock.patch.object(
                module.migration,
                "recover_migration",
                side_effect=injected,
            ):
                with self.assertRaises(LoopMemoryError) as context:
                    module._check_pending_migrations(root)

            self.assertEqual(context.exception.code, "source_changed")
            self.assertIs(context.exception.recoverable, False)

    def test_legacy_candidate_symlink_is_rejected_before_alias_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            real_source = temp / "real" / ".memory"
            real_source.mkdir(parents=True)
            (real_source / "legacy.md").write_text(SECRET, encoding="utf-8")
            cwd.mkdir()
            legacy_link = cwd / ".memory"
            legacy_link.symlink_to(real_source, target_is_directory=True)
            store = RegistryStore(root)
            store.initialize()
            store.add_legacy_alias(
                real_source,
                "projects/p-do-not-use",
                "m-" + "1" * 32,
            )

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-symlink",
                "--root",
                root,
                "--json",
            )

            self.assert_failure(completed, 4, "unsafe_legacy_source")
            self.assertFalse((root / "projects").exists())
            self.assertFalse((root / "global").exists())
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)

    def test_unexpected_manifest_entry_blocks_after_identity_before_body_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            RegistryStore(root).initialize()
            manifests = root / "migrations" / "manifests"
            manifests.mkdir(parents=True)
            (manifests / "garbage.tmp").write_text(
                "unexpected metadata",
                encoding="utf-8",
            )
            body = temp / "status.md"
            body.write_text(f"# Session Status\n\n{SECRET}\n", encoding="utf-8")

            completed = self.run_cli(
                "session-write",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-garbage",
                "--kind",
                "status",
                "--input",
                body,
                "--root",
                root,
                "--json",
            )

            self.assert_failure(completed, 4, "corrupt_state")
            state = json.loads((root / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["projects"]), 1)
            # Namespace corruption is rejected before identity publication;
            # no orphan session record may survive without a materialized tree.
            self.assertEqual(len(state["sessions"]), 0)
            self.assertFalse((root / "projects").exists())
            self.assertFalse((root / "global").exists())
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)

    def test_current_project_legacy_memory_requires_staging_without_following_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            root = temp / "loop"
            cwd = temp / "project"
            legacy = cwd / ".memory"
            legacy.mkdir(parents=True)
            outside = temp / "outside-secret.md"
            outside.write_text(SECRET, encoding="utf-8")
            (legacy / "legacy.md").write_text(
                f"# Legacy\n\nUntrusted path: {outside}\n",
                encoding="utf-8",
            )

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-legacy",
                "--root",
                root,
                "--json",
            )

            payload = self.assert_success(completed, "preflight", root)
            self.assertFalse(payload["capabilities"]["project_promote"])
            self.assertTrue(payload["capabilities"]["session_write"])
            self.assertFalse((root / "migrations/manifests").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), SECRET)
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)

    def test_explicit_nondefault_root_does_not_scan_default_global_legacy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            global_legacy = home / ".codex/.memory"
            global_legacy.mkdir(parents=True)
            (global_legacy / "long.md").write_text(SECRET, encoding="utf-8")
            root = temp / "explicit-loop"
            cwd = temp / "project"
            cwd.mkdir()

            completed = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-explicit-root",
                "--root",
                root,
                "--json",
                home=home,
            )

            self.assert_success(completed, "preflight", root)
            self.assertTrue(global_legacy.is_dir())
            self.assertFalse((root / "migrations/manifests").exists())
            self.assertNotIn(SECRET, completed.stdout)

    def test_default_root_detects_global_legacy_and_holds_validated_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            legacy = home / ".codex/.memory"
            legacy.mkdir(parents=True)
            (legacy / "long.md").write_text(
                "# Global Long-Term Memory\n\n## Entries\n\n"
                "- [2026-08-10][verified] Legacy global fact.\n"
                "  Evidence: tests/test_cli.py\n",
                encoding="utf-8",
            )
            cwd = temp / "project"
            cwd.mkdir()
            root = home / "loop-memory"

            detected = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-global",
                "--json",
                home=home,
            )
            self.assert_failure(detected, 3, "migration_required")
            manifest_path = next((root / "migrations/manifests").glob("*.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            classification = temp / "classification.json"
            classification.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "long.md",
                                "destination": "global/long.md",
                                "mode": "merge_entries",
                            }
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )
            held = self.assert_success(
                self.run_cli(
                    "migrate-apply",
                    "--manifest",
                    manifest_path,
                    "--classification",
                    classification,
                    "--stop-after",
                    "validated",
                    "--json",
                    home=home,
                ),
                "migrate-apply",
                root,
            )
            self.assertEqual(held["migration"]["state"], "validated")
            self.assertEqual(held["migration"]["hold_reason"], "governance_switch")

            blocked = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-global",
                "--json",
                home=home,
            )
            self.assert_failure(blocked, 3, "migration_required")
            unchanged = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(unchanged["state"], "validated")
            self.assertEqual(unchanged["hold_reason"], "governance_switch")
            self.assertTrue(legacy.is_dir())

    def test_reserved_product_root_and_explicit_candidate_are_never_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            product = home / ".codex/memories"
            candidate = product / "fixture/.memory"
            candidate.mkdir(parents=True)
            (candidate / "body.md").write_text(SECRET, encoding="utf-8")
            cwd = temp / "project"
            cwd.mkdir()
            root = temp / "loop"

            scanned = self.assert_success(
                self.run_cli(
                    "migrate-scan",
                    "--cwd",
                    cwd,
                    "--legacy-path",
                    candidate,
                    "--root",
                    root,
                    "--json",
                    home=home,
                ),
                "migrate-scan",
                root,
            )
            self.assertEqual(scanned["manifests"], [])
            self.assertEqual(
                scanned["excluded"],
                [
                    {
                        "path": str(candidate),
                        "reason": "reserved_product_memory",
                    }
                ],
            )
            self.assertTrue(candidate.is_dir())
            self.assertNotIn(SECRET, json.dumps(scanned))

            rejected = self.run_cli(
                "initialize",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-product",
                "--root",
                product,
                "--json",
                home=home,
            )
            self.assert_failure(rejected, 4, "reserved_product_memory")

            refresh_rejected = self.run_cli(
                "migrate-refresh",
                "--manifest",
                candidate / "body.md",
                "--root",
                product,
                "--json",
                home=home,
            )
            self.assert_failure(
                refresh_rejected,
                4,
                "reserved_product_memory",
            )
            self.assertNotIn(SECRET, refresh_rejected.stdout)
            self.assertNotIn(SECRET, refresh_rejected.stderr)
            self.assertFalse((product / "registry.json").exists())


class RootSafetyTests(CliTestCase):
    def test_default_root_symlink_is_rejected_without_writing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            cwd = temp / "project"
            target = temp / "target"
            home.mkdir()
            cwd.mkdir()
            target.mkdir()
            (home / "loop-memory").symlink_to(target, target_is_directory=True)

            completed = self.run_cli(
                "initialize",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-default-symlink",
                "--json",
                home=home,
            )

            self.assert_failure(completed, 4, "unsafe_path")
            self.assertFalse((target / "registry.json").exists())

    def test_system_assigned_mode_is_accepted_and_symlink_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            cwd = temp / "project"
            cwd.mkdir()
            permissive = temp / "permissive"
            permissive.mkdir()
            target = temp / "target"
            target.mkdir()
            symlink = temp / "symlink-root"
            symlink.symlink_to(target, target_is_directory=True)

            accepted = self.run_cli(
                "initialize",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-root-safety",
                "--root",
                permissive,
                "--json",
            )
            self.assert_success(accepted, "initialize", permissive)

            rejected = self.run_cli(
                "initialize",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-root-safety",
                "--root",
                symlink,
                "--json",
            )
            self.assert_failure(rejected, 4, "unsafe_path")
            self.assertFalse((target / "registry.json").exists())

    def test_wrong_owner_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve() / "loop"
            root.mkdir()
            specification = importlib.util.spec_from_file_location(
                "loop_memory_cli_for_owner_test",
                SCRIPT,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)

            with mock.patch.object(module.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(module.LoopMemoryError) as context:
                    module._prepare_root(root, initialize=True)

            self.assertEqual(context.exception.code, "invalid_root_owner")
            self.assertFalse((root / "registry.json").exists())
            self.assertFalse((root / "locks").exists())


if __name__ == "__main__":
    unittest.main()
