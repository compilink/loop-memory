import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

from scripts.loopmem.capabilities import Capabilities, Notice
from scripts.loopmem import convergence
from scripts.loopmem.convergence import ConvergenceCacheKey, evaluate_capabilities
from scripts.loopmem.errors import AccessDenied
from scripts.loopmem.registry import RegistryStore
from scripts.loopmem.root import convert_v1_metadata, publish_conversion


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop_memory.py"


class ConvergenceCliTests(unittest.TestCase):
    def authority(self, root: Path) -> None:
        RegistryStore(root).initialize_v2()
        publish_conversion(root, convert_v1_metadata(root))

    def run_cli(self, *args: object, home: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(str(a) for a in args)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def payload(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        return json.loads(result.stdout)

    def test_enter_preflight_and_initialize_share_one_orchestrator(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            results = [
                self.payload(self.run_cli(op, "--cwd", project, "--session-id", "host", "--root", home / "loop", "--json", home=home))
                for op in ("enter", "preflight", "initialize")
            ]
            self.assertTrue(all(item["ok"] is True for item in results))
            self.assertEqual({item["operation"] for item in results}, {"enter", "preflight", "initialize"})
            for item in results:
                self.assertIn("capabilities", item)
                self.assertIn("notices", item)

    def test_clean_root_has_all_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            item = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", home / "loop", "--json", home=home))
            self.assertTrue(item["ok"])
            self.assertFalse(item["degraded"])
            self.assertTrue(all(item["capabilities"].values()))

    def test_unresolved_outbox_disables_only_close(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            outbox = Path(first["paths"]["agent_outbox"])
            outbox.write_text("# Main Agent Outbox\n\n- candidate\n", encoding="utf-8")
            second = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            self.assertTrue(second["capabilities"]["session_read"])
            self.assertTrue(second["capabilities"]["session_write"])
            self.assertFalse(second["capabilities"]["session_close"])

    def test_cached_enter_still_observes_new_subagent_outbox(self):
        import scripts.loop_memory as cli
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve(); project = home / "project"; project.mkdir(); root = home / "loop"
            first = cli._identity_preflight(root, str(project), "host", None)
            subagent = Path(first["paths"]["session"]) / "agents/subagents/worker"
            subagent.mkdir()
            (subagent / "outbox.md").write_text("# Subagent Outbox\n\n- pending\n")
            second = cli._identity_preflight(root, str(project), "host", None)
            self.assertFalse(second["capabilities"]["session_close"])

    def test_credential_current_legacy_keeps_session_and_global_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            legacy = project / ".memory"
            legacy.mkdir(parents=True)
            (legacy / "project.md").write_text("SERVICE_TOKEN=secret-never-output\n")
            item = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", home / "loop", "--json", home=home))
            self.assertTrue(item["ok"])
            self.assertTrue(item["degraded"])
            self.assertFalse(item["capabilities"]["project_read"])
            self.assertFalse(item["capabilities"]["project_promote"])
            self.assertFalse(item["capabilities"]["migration_apply"])
            self.assertTrue(item["capabilities"]["global_promote"])
            self.assertTrue(item["capabilities"]["session_write"])
            self.assertNotIn("secret-never-output", json.dumps(item))

    def test_missing_current_custody_snapshot_degrades_project_but_keeps_session(self):
        from scripts.loopmem import migration as migration_module

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            legacy = project / ".memory"
            legacy.mkdir(parents=True)
            (legacy / "project.md").write_text("SERVICE_TOKEN=secret-never-output\n")
            root = home / "loop"

            scan = migration_module.scan_legacy(root, project, [])
            manifest_path = Path(scan["manifests"][0])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshot = Path(manifest["snapshot"])
            if not snapshot.is_absolute():
                snapshot = root / snapshot
            shutil.rmtree(snapshot.parent)

            entered = self.payload(self.run_cli(
                "enter",
                "--cwd",
                project,
                "--session-id",
                "host",
                "--root",
                root,
                "--json",
                home=home,
            ))

            self.assertTrue(entered["ok"])
            self.assertTrue(entered["capabilities"]["session_write"])
            self.assertFalse(entered["capabilities"]["project_read"])
            self.assertIn(
                "protected_current_project_legacy",
                {notice["code"] for notice in entered["notices"]},
            )
            self.assertNotIn("secret-never-output", json.dumps(entered))

    def test_ambiguous_current_legacy_disables_only_project_promotion(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            legacy = project / ".memory"
            legacy.mkdir(parents=True)
            (legacy / "project.md").write_text("ordinary legacy fact\n")
            item = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", home / "loop", "--json", home=home))
            self.assertTrue(item["ok"])
            self.assertTrue(item["capabilities"]["project_read"])
            self.assertFalse(item["capabilities"]["project_promote"])
            self.assertTrue(item["capabilities"]["migration_apply"])

    def test_mutations_reenter_and_reject_only_their_exact_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            Path(first["paths"]["agent_outbox"]).write_text(
                "# Main Agent Outbox\n\n- unresolved\n",
                encoding="utf-8",
            )
            status = home / "status.md"
            status.write_text("# Session Status\n\nstill active\n")
            write = self.run_cli(
                "session-write", "--cwd", project, "--thread-id", "host",
                "--kind", "status", "--input", status, "--root", root,
                "--json", home=home,
            )
            self.assertTrue(self.payload(write)["ok"])
            close = self.run_cli(
                "session-close", "--cwd", project, "--thread-id", "host",
                "--root", root, "--json", home=home,
            )
            closed = self.payload(close)
            self.assertFalse(closed["ok"])
            self.assertEqual(closed["error"]["code"], "capability_denied")

            legacy = project / ".memory"
            legacy.mkdir()
            (legacy / "project.md").write_text("ambiguous legacy fact\n")
            entry = home / "entry.md"
            entry.write_text(
                "- [2026-08-14][verified] Candidate.\n"
                "  Evidence: tests/test_convergence.py\n"
            )
            project_promote = self.run_cli(
                "promote", "--cwd", project, "--thread-id", "host",
                "--scope", "project", "--section", "Verified Facts",
                "--input", entry, "--root", root, "--json", home=home,
            )
            denied = self.payload(project_promote)
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["error"]["code"], "capability_denied")
            global_promote = self.run_cli(
                "promote", "--cwd", project, "--thread-id", "host",
                "--scope", "global-long", "--section", "Methodology",
                "--input", entry, "--root", root, "--json", home=home,
            )
            self.assertTrue(self.payload(global_promote)["ok"])

    def test_corrupt_registry_returns_global_failure_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            root.mkdir()
            (root / "registry.json").write_text("not-json\n", encoding="utf-8")
            before = (root / "registry.json").read_bytes()
            result = self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home)
            item = self.payload(result)
            self.assertFalse(item["ok"])
            self.assertEqual(item["error"]["code"], "corrupt_state")
            self.assertEqual((root / "registry.json").read_bytes(), before)
            self.assertFalse((root / "projects").exists())

    def test_default_enter_transactionally_relocates_the_only_legacy_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            old = home / ".codex" / "loop-memory"
            new = home / "loop-memory"
            self.authority(old)
            old_id = json.loads((old / "root.json").read_text())["root_id"]
            result = self.payload(self.run_cli(
                "enter", "--cwd", project, "--session-id", "host", "--json",
                home=home,
            ))
            self.assertTrue(result["ok"])
            self.assertEqual(result["root"], str(new))
            self.assertFalse(old.exists())
            self.assertEqual(json.loads((new / "root.json").read_text())["root_id"], old_id)

    def test_completed_default_relocation_does_not_reacquire_parent_lock(self):
        """A completed relocation is ordinary entry, not another migration."""
        import scripts.loop_memory as cli

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop-memory"
            self.authority(root)
            root_id = json.loads((root / "root.json").read_text())["root_id"]
            (root / "relocation.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "phase": "complete",
                    "old_root": str(home / ".codex" / "loop-memory"),
                    "new_root": str(root),
                    "root_id": root_id,
                }, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(cli, "default_loop_root", return_value=root),
                mock.patch.object(
                    cli,
                    "legacy_loop_root",
                    return_value=home / ".codex" / "loop-memory",
                ),
                mock.patch.object(
                    cli.root_module,
                    "_WaitingRelocationLease",
                    side_effect=AssertionError("completed relocation must not reacquire a lock"),
                ),
            ):
                identity = cli._identity_preflight(
                    root,
                    str(project),
                    "host",
                    None,
                    str(project),
                )

            self.assertTrue(identity["capabilities"]["session_write"])
            self.assertFalse((home / ".loop-memory-relocation.lock").exists())

    def test_two_authority_roots_fail_closed_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            old = home / ".codex" / "loop-memory"
            new = home / "loop-memory"
            self.authority(old)
            self.authority(new)
            old_before = (old / "root.json").read_bytes()
            new_before = (new / "root.json").read_bytes()
            result = self.payload(self.run_cli(
                "enter", "--cwd", project, "--session-id", "host", "--json",
                home=home,
            ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "root_conflict")
            self.assertEqual((old / "root.json").read_bytes(), old_before)
            self.assertEqual((new / "root.json").read_bytes(), new_before)
            self.assertFalse((new / "projects").exists())

    def test_missing_lazy_session_file_remains_absent_on_enter(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            status = Path(first["paths"]["status"])
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text("# Session Status\nwork\n", encoding="utf-8")
            status.unlink()
            entered = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            self.assertTrue(entered["ok"])
            self.assertFalse(status.exists())
            self.assertFalse((root / "root.transaction.json").exists())

    def test_expired_provably_dead_registry_lock_is_repaired(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            lock = root / "locks" / "registry.lock"
            lock.write_text(json.dumps({
                "owner": "dead-test",
                "pid": 99999999,
                "acquired_at": 1,
                "expires_at": 2,
                "token": "deadbeef",
            }) + "\n")
            result = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host-2", "--root", root, "--json", home=home))
            self.assertTrue(result["ok"])
            self.assertFalse(lock.exists())

    def test_uncertain_live_lock_fails_closed_without_scope_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "host", "--root", root, "--json", home=home))
            lock = root / "locks" / "registry.lock"
            lock.write_text(json.dumps({
                "owner": "live-test",
                "pid": os.getpid(),
                "acquired_at": time.time(),
                "expires_at": time.time() + 120,
                "token": "live-token",
            }) + "\n")
            result = self.payload(self.run_cli("enter", "--cwd", project, "--session-id", "new-host", "--root", root, "--json", home=home))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "lease_busy")
            self.assertTrue(lock.exists())


class CapabilityValueTests(unittest.TestCase):
    def test_capabilities_and_notice_are_immutable_and_serializable(self):
        value = Capabilities()
        self.assertTrue(value.global_read)
        self.assertEqual(set(value.as_dict()), {
            "global_read", "global_promote", "project_read", "project_promote",
            "session_read", "session_write", "session_close", "migration_apply",
        })
        with self.assertRaises(FrozenInstanceError):
            value.global_read = False
        notice = Notice("x", "project", ("project_promote",), "repair")
        self.assertEqual(notice.as_dict()["blocking"], ["project_promote"])
        with self.assertRaises(FrozenInstanceError):
            notice.code = "y"

    def test_credential_current_project_legacy_degrades_only_related_scope(self):
        capabilities, notices = evaluate_capabilities(
            protected_current_project_legacy=True,
            credential_current_project_legacy=True,
        )
        self.assertFalse(capabilities.project_read)
        self.assertFalse(capabilities.project_promote)
        self.assertFalse(capabilities.migration_apply)
        self.assertTrue(capabilities.global_read)
        self.assertTrue(capabilities.global_promote)
        self.assertTrue(capabilities.session_read)
        self.assertTrue(capabilities.session_write)
        self.assertTrue(capabilities.session_close)
        self.assertEqual(notices[0].scope, "project")

    def test_ambiguous_project_facts_disable_only_project_promotion(self):
        capabilities, notices = evaluate_capabilities(ambiguous_project_facts=True)
        self.assertFalse(capabilities.project_promote)
        self.assertTrue(capabilities.project_read)
        self.assertTrue(capabilities.migration_apply)
        self.assertEqual(notices[0].blocking, ("project_promote",))

    def test_content_conflict_keeps_project_read_and_blocks_only_related_writes(self):
        capabilities, notices = evaluate_capabilities(
            ambiguous_project_facts=True,
            migration_conflict=True,
        )
        self.assertTrue(capabilities.project_read)
        self.assertFalse(capabilities.project_promote)
        self.assertFalse(capabilities.migration_apply)
        self.assertTrue(capabilities.session_write)
        self.assertTrue(capabilities.global_promote)
        self.assertEqual(
            {item.code for item in notices},
            {"ambiguous_project_facts", "migration_conflict"},
        )


class EnterOrderingTests(unittest.TestCase):
    def test_access_denial_precedes_relocation_and_legacy_inspection_without_writes(self):
        import scripts.loop_memory as cli

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            cwd = base / "project"
            cwd.mkdir()
            root = base / "missing-loop"
            with (
                mock.patch.object(cli.access, "check_access", side_effect=AccessDenied()) as access_check,
                mock.patch.object(cli.root_module, "relocate_root") as relocate,
                mock.patch.object(cli.migration, "inspect_project_legacy_source") as inspect,
            ):
                with self.assertRaises(AccessDenied):
                    cli._identity_preflight(root, str(cwd), "host", None)
            access_check.assert_called_once()
            relocate.assert_not_called()
            inspect.assert_not_called()
            self.assertFalse(root.exists())

    def test_missing_destination_access_probe_does_not_materialize_root(self):
        from scripts.loopmem import access

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "missing-loop"
            access.check_access(root, materialize_missing=False)
            self.assertFalse(root.exists())

    def test_maintain_does_not_recover_pending_migrations_or_initialize_root(self):
        import scripts.loop_memory as cli

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "loop"
            root.mkdir()
            with (
                mock.patch.object(cli, "_check_pending_migrations") as pending,
                mock.patch.object(cli, "_prepare_root", wraps=cli._prepare_root) as prepare,
            ):
                args = type("Args", (), {"operation": "maintain", "root": str(root), "now": 1.0})()
                # A missing registry is a read-only diagnosis boundary here; the
                # assertion is about avoiding migration recovery side effects.
                with self.assertRaises(Exception):
                    cli._dispatch(args)
            pending.assert_not_called()
            prepare.assert_called_once_with(root, initialize=False)

    def test_enter_uses_matching_scope_cache_and_any_key_change_rescans(self):
        import scripts.loop_memory as cli

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            cwd = base / "project"
            cwd.mkdir()
            root = base / "loop"
            with mock.patch.object(cli, "_scan_current_scope", wraps=cli._scan_current_scope) as scan:
                cli._identity_preflight(root, str(cwd), "host", None)
                first = scan.call_count
                cli._identity_preflight(root, str(cwd), "host", None)
                self.assertEqual(scan.call_count, first)
                session = next((root / "projects").glob("*/sessions/active/*"))
                status = session / "status.md"
                status.write_text("# Session Status\nchanged\n")
                cli._identity_preflight(root, str(cwd), "host", None)
                self.assertGreater(scan.call_count, first)

    def test_cache_key_each_authority_field_invalidates_scope_scan(self):
        base = ConvergenceCacheKey("r", 1, 1, "/cwd", "/project", "a" * 64)
        for field, value in (
            ("root_id", "r2"), ("root_generation", 2),
            ("registry_generation", 2), ("cwd", "/other"),
            ("project_root", "/other-project"), ("scope_digest", "b" * 64),
        ):
            with self.subTest(field=field):
                changed = ConvergenceCacheKey(**{**base.as_dict(), field: value})
                self.assertFalse(base.matches(changed))


    def test_unrelated_project_migration_is_notice_only(self):
        capabilities, notices = evaluate_capabilities(unrelated_project_migration=True)
        self.assertTrue(all(capabilities.as_dict().values()))
        self.assertEqual(notices[0].scope, "other-project")
        self.assertEqual(notices[0].blocking, ())

    def test_fast_path_key_includes_every_authoritative_invalidation_field(self):
        base = ConvergenceCacheKey(
            root_id="r-one",
            root_generation=1,
            registry_generation=2,
            cwd="/work/project/subdir",
            project_root="/work/project",
            scope_digest="a" * 64,
        )
        self.assertTrue(base.matches(base))
        replacements = (
            {"root_id": "r-two"},
            {"root_generation": 2},
            {"registry_generation": 3},
            {"cwd": "/work/project/other"},
            {"project_root": "/work/other"},
            {"scope_digest": "b" * 64},
        )
        for update in replacements:
            with self.subTest(update=update):
                changed = ConvergenceCacheKey(**{**base.as_dict(), **update})
                self.assertFalse(base.matches(changed))
