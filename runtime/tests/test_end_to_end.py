import importlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import migration as migration_module
from scripts.loopmem.paths import discover_project
from scripts.loopmem.registry import RegistryStore
from scripts.loopmem.sessions import (
    archive_session,
    ensure_project_layout,
    ensure_session_layout,
    promote_entry,
    write_session_file,
)
from scripts.loopmem.storage import FileLease


DAY = 24 * 60 * 60
NOW = 200 * DAY
CUTOFF = NOW - 90 * DAY
SECRET = "SENTINEL-SECRET-TEXT-DO-NOT-LEAK"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop_memory.py"


class MaintenanceEndToEndTests(unittest.TestCase):
    def maintenance_module(self):
        try:
            return importlib.import_module("scripts.loopmem.maintenance")
        except ModuleNotFoundError:
            self.fail("scripts.loopmem.maintenance has not been implemented")

    def assert_loop_error(self, code: str, operation) -> LoopMemoryError:
        with self.assertRaises(LoopMemoryError) as context:
            operation()
        self.assertEqual(context.exception.code, code)
        return context.exception

    def test_enter_resumes_archived_platform_session_as_generation_two(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            temp = Path(temp_dir)
            home = temp / "home"
            home.mkdir()
            project = home / "project"
            project.mkdir()
            root = home / "loop"

            def run(*args: str) -> dict[str, object]:
                env = os.environ.copy()
                env["HOME"] = str(home)
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), *args, "--json"],
                    env=env, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            first = run("enter", "--cwd", str(project), "--session-id", "host", "--root", str(root))
            first_handoff = Path(first["paths"]["handoff"])
            first_handoff.write_text("generation one handoff\n", encoding="utf-8")
            closed = run("session-close", "--cwd", str(project), "--thread-id", "host", "--root", str(root))
            first_archive = Path(closed["path"])
            second = run("enter", "--cwd", str(project), "--session-id", "host", "--root", str(root))
            self.assertNotEqual(second["session_id"], first["session_id"])
            self.assertEqual(second["session_generation"], 2)
            self.assertEqual(second["resumes_from"], first["session_id"])
            self.assertEqual(second["resume_handoff"], str(first_archive / "handoff.md"))
            self.assertTrue(first_archive.is_dir())
            self.assertFalse(first_archive.is_symlink())
            self.assertEqual(run("enter", "--cwd", str(project), "--session-id", "host", "--root", str(root))["session_id"], second["session_id"])
            new_status = home / "new-status.md"
            new_status.write_text("generation two only\n", encoding="utf-8")
            run("session-write", "--cwd", str(project), "--thread-id", "host", "--kind", "status", "--input", str(new_status), "--root", str(root))
            self.assertEqual((Path(second["paths"]["status"])).read_text(), "generation two only\n")
            self.assertEqual((first_archive / "handoff.md").read_text(), "generation one handoff\n")
            second_closed = run("session-close", "--cwd", str(project), "--thread-id", "host", "--root", str(root))
            self.assertNotEqual(Path(second_closed["path"]), first_archive)
            self.assertEqual((Path(second_closed["path"]) / "status.md").read_text(), "generation two only\n")
            self.assertEqual((first_archive / "handoff.md").read_text(), "generation one handoff\n")

    def test_enter_reinitializes_registry_only_active_session_once(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            temp = Path(temp_dir)
            home = temp / "home"
            home.mkdir()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            env = os.environ.copy()
            env["HOME"] = str(home)

            def run() -> dict[str, object]:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "enter",
                        "--cwd",
                        str(project),
                        "--session-id",
                        "host",
                        "--root",
                        str(root),
                        "--json",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            first = run()
            status = Path(first["paths"]["status"])
            status.write_text("Goal: recover\n", encoding="utf-8")
            shutil.rmtree(Path(first["paths"]["session"]))

            recovered = run()

            self.assertEqual(recovered["session_id"], first["session_id"])
            self.assertEqual(
                recovered["session_generation"], first["session_generation"]
            )
            self.assertTrue(recovered["session_recovered"])
            self.assertIn(
                "session_memory_reinitialized",
                {notice["code"] for notice in recovered["notices"]},
            )
            self.assertFalse(Path(recovered["paths"]["status"]).exists())

            repeated = run()
            self.assertNotIn("session_recovered", repeated)
            self.assertNotIn(
                "session_memory_reinitialized",
                {notice["code"] for notice in repeated["notices"]},
            )

    def test_unresolved_outbox_keeps_generation_read_write_but_denies_close(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            temp = Path(temp_dir)
            home = temp / "home"
            home.mkdir()
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            env = os.environ.copy()
            env["HOME"] = str(home)

            def run(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(SCRIPT), *args, "--json"],
                    env=env, capture_output=True, text=True, check=False,
                )

            entered = run("enter", "--cwd", str(project), "--session-id", "host", "--root", str(root))
            first = json.loads(entered.stdout)
            outbox = Path(first["paths"]["agent_outbox"])
            outbox.write_text("# Main Agent Outbox\n\n- unresolved\n", encoding="utf-8")
            current = run("enter", "--cwd", str(project), "--session-id", "host", "--root", str(root))
            payload = json.loads(current.stdout)
            self.assertTrue(payload["capabilities"]["session_read"])
            self.assertTrue(payload["capabilities"]["session_write"])
            self.assertFalse(payload["capabilities"]["session_close"])
            status = home / "status.md"
            status.write_text("still writable\n", encoding="utf-8")
            write = run("session-write", "--cwd", str(project), "--thread-id", "host", "--kind", "status", "--input", str(status), "--root", str(root))
            self.assertEqual(write.returncode, 0, write.stderr)
            close = run("session-close", "--cwd", str(project), "--thread-id", "host", "--root", str(root))
            self.assertNotEqual(close.returncode, 0)
            self.assertEqual(json.loads(close.stdout)["error"]["code"], "capability_denied")

    def test_enter_registry_publish_failure_rolls_back_only_owned_templates(self):
        import scripts.loop_memory as cli
        import scripts.loopmem.registry as registry_module
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            temp = Path(temp_dir)
            home = temp / "home"
            project = home / "project"
            project.mkdir(parents=True)
            root = home / "loop"
            real_write = registry_module.write_json_atomic
            failed = False

            def fail_session_publish(path, state):
                nonlocal failed
                if state.get("sessions") and not failed:
                    failed = True
                    raise OSError("injected registry publish failure")
                return real_write(path, state)

            with mock.patch.object(
                registry_module, "write_json_atomic", side_effect=fail_session_publish
            ):
                with self.assertRaisesRegex(OSError, "injected registry publish failure"):
                    cli._identity_preflight(root, str(project), "host", None)

            state = json.loads((root / "registry.json").read_text())
            project_id = next(iter(state["projects"]))
            active = root / "projects" / project_id / "sessions" / "active"
            self.assertEqual(state["sessions"], {})
            self.assertEqual(list(active.iterdir()), [])
            identity = cli._identity_preflight(root, str(project), "host", None)
            self.assertEqual(len(list(active.iterdir())), 1)
            self.assertEqual(next(active.iterdir()).name, identity["session_id"])

    def archive(
        self,
        loop_root: Path,
        session_id: str,
        *,
        main_outbox: str | None = None,
        subagent_outbox: str | None = None,
        mtime: float,
    ) -> Path:
        if not (loop_root / "registry.json").exists():
            RegistryStore(loop_root).initialize()
        ensure_session_layout(loop_root, "p-project", session_id)
        if main_outbox is not None:
            write_session_file(
                loop_root,
                "p-project",
                session_id,
                "outbox",
                main_outbox,
            )
        if subagent_outbox is not None:
            write_session_file(
                loop_root,
                "p-project",
                session_id,
                "outbox",
                subagent_outbox,
                "worker",
            )
        destination = archive_session(
            loop_root,
            "p-project",
            session_id,
        )
        os.utime(destination, (mtime, mtime))
        return destination

    def write_manifest(
        self,
        loop_root: Path,
        migration_id: str,
        state: str,
        *,
        hold: str | None = None,
        protected: bool = False,
        quarantine_path: Path | None = None,
        staging_path: Path | None = None,
    ) -> Path:
        manifests = loop_root / "migrations/manifests"
        manifests.mkdir(parents=True, exist_ok=True)
        source = (
            loop_root.parent / f"legacy-{migration_id[2:]}" / ".memory"
        ).resolve()
        target = (loop_root / "projects/p-project").resolve()
        value: dict[str, object] = {
            "migration_id": migration_id,
            "schema_version": 1,
            "state": state,
            "source": str(source),
            "source_kind": "empty",
            "project_id": "p-project",
            "catalogued_files": [],
            "files": [],
            "target": str(target),
            "created_at": 1,
            "updated_at": 2,
            "warnings": [],
        }
        copied_or_later = state in {
            "copied",
            "validated",
            "references_updated",
            "quarantined",
            "complete",
        }
        if copied_or_later:
            value.update(
                {
                    "classification_sha256": "a" * 64,
                    "target_files": [],
                    "staging_path": str(
                        (
                            staging_path
                            or loop_root / "migrations/staging" / migration_id
                        ).resolve()
                    ),
                    "publish_plan_sha256": "b" * 64,
                }
            )
        if hold is not None:
            value["hold_reason"] = hold
        if state in {"quarantined", "complete"}:
            value["quarantine_path"] = str(
                (
                    quarantine_path
                    or loop_root
                    / "migrations/quarantine"
                    / migration_id
                    / "source"
                ).resolve()
            )
        if protected:
            value["protected"] = True
            value["protection_reasons"] = ["credential_assignment"]
        path = manifests / f"{migration_id}.json"
        path.write_text(
            json.dumps(value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def migration_directories(
        self,
        loop_root: Path,
        migration_id: str,
        *,
        mtime: float,
    ) -> tuple[Path, Path]:
        quarantine = loop_root / "migrations/quarantine" / migration_id
        (quarantine / "source").mkdir(parents=True)
        staging = loop_root / "migrations/staging" / migration_id
        staging.mkdir(parents=True)
        (staging / "publish-plan.json").write_text("{}\n", encoding="utf-8")
        os.utime(quarantine, (mtime, mtime))
        os.utime(staging, (mtime, mtime))
        return quarantine.resolve(), staging.resolve()

    def complete_project_migration(
        self,
        temp: Path,
        *,
        loop_root: Path | None = None,
        name: str = "project-migration",
        protected: bool = False,
    ) -> dict[str, object]:
        root = loop_root or temp / "loop"
        cwd = temp / name
        source = cwd / ".memory"
        source.mkdir(parents=True)
        imported = f"- imported fact for {name}"
        source_risk = "\nSERVICE_TOKEN=fixture-value" if protected else ""
        (source / "legacy.md").write_text(
            f"# Legacy\n\n## Entries\n\n{imported}{source_risk}\n",
            encoding="utf-8",
        )
        scan = migration_module.scan_legacy(root, cwd, [])
        manifest_path = Path(scan["manifests"][0])
        manifest = migration_module.load_manifest(manifest_path)
        classification = {
            "migration_id": manifest["migration_id"],
            "actions": [
                {
                    "source": "legacy.md",
                    "destination": "project/project.md",
                    "mode": "merge_entries",
                    "section": "Verified Facts",
                }
            ],
            "reference_updates": [],
        }
        if protected:
            classification["approved_protected"] = True
        classification_path = temp / f"{name}-classification.json"
        classification_path.write_text(
            json.dumps(classification),
            encoding="utf-8",
        )
        completed = migration_module.apply_migration(
            root,
            manifest_path,
            classification_path,
        )
        # Maintenance compatibility tests exercise an old, already-internal
        # schema-v1 quarantine. Current schema-v2 snapshots are retained until
        # explicit legacy-delete and must never become age-based cleanup input.
        migration_id = completed["migration_id"]
        quarantine = root / "migrations" / "quarantine" / migration_id
        shutil.copytree(Path(completed["snapshot"]), quarantine / "source")
        compat_source = (cwd / "retired-v1" / ".memory").resolve()
        RegistryStore(root).add_legacy_alias(
            compat_source,
            completed["target"],
            migration_id,
        )
        compat_manifest = {
            key: value
            for key, value in completed.items()
            if key not in {"snapshot", "source_inventory_sha256"}
        }
        compat_manifest.update(
            {
                "schema_version": 1,
                "source": str(compat_source),
                "target": str(Path(completed["target"])),
                "staging_path": str(Path(completed["staging_path"])),
                "quarantine_path": str((quarantine / "source").resolve()),
            }
        )
        migration_module.write_json_atomic(manifest_path, compat_manifest)
        completed = migration_module.load_manifest(manifest_path)
        staging = Path(completed["staging_path"])
        os.utime(quarantine, (CUTOFF - DAY, CUTOFF - DAY))
        os.utime(staging, (CUTOFF - DAY, CUTOFF - DAY))
        return {
            "loop_root": root.resolve(),
            "manifest_path": manifest_path.resolve(),
            "manifest": completed,
            "quarantine": quarantine.resolve(),
            "staging": staging.resolve(),
            "target_file": Path(completed["target"]) / "project.md",
            "imported": imported,
        }

    def test_invalid_now_is_rejected_before_root_access(self):
        maintenance = self.maintenance_module()
        self.assertEqual(maintenance.RETENTION_DAYS, 90)
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_root = Path(temp_dir) / "missing"
            for value in (-1, math.nan, math.inf, -math.inf, True, "1"):
                with self.subTest(value=value):
                    self.assert_loop_error(
                        "invalid_now",
                        lambda value=value: maintenance.maintain(
                            missing_root,
                            value,
                        ),
                    )
            self.assertFalse(missing_root.exists())

    def test_missing_corrupt_or_unsupported_registry_blocks_all_deletion(self):
        maintenance = self.maintenance_module()
        cases = {
            "missing": "corrupt_state",
            "corrupt": "corrupt_state",
            "unsupported": "unsupported_schema",
        }
        for case, expected_code in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                loop_root = Path(temp_dir) / "loop"
                archived = self.archive(
                    loop_root,
                    "s-old",
                    mtime=CUTOFF - DAY,
                )
                registry = loop_root / "registry.json"
                if case == "missing":
                    registry.unlink()
                elif case == "corrupt":
                    registry.write_text(
                        json.dumps({"injected": SECRET}),
                        encoding="utf-8",
                    )
                else:
                    state = json.loads(registry.read_text(encoding="utf-8"))
                    state["schema_version"] = 2
                    registry.write_text(
                        json.dumps(state, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                self.assert_loop_error(
                    expected_code,
                    lambda: maintenance.maintain(loop_root, NOW),
                )

                self.assertTrue(archived.is_dir())

    def test_archive_retention_boundary_outboxes_and_active_are_fail_closed(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            old_empty = self.archive(
                loop_root,
                "s-old-empty",
                mtime=CUTOFF - 1,
            )
            boundary = self.archive(
                loop_root,
                "s-boundary",
                mtime=CUTOFF,
            )
            unresolved_main = self.archive(
                loop_root,
                "s-unresolved-main",
                main_outbox=f"# Main Agent Outbox\n\n{SECRET}\n",
                mtime=CUTOFF - 1,
            )
            unresolved_subagent = self.archive(
                loop_root,
                "s-unresolved-subagent",
                subagent_outbox=f"# Subagent Outbox\n\n{SECRET}\n",
                mtime=CUTOFF - 1,
            )
            template_only = self.archive(
                loop_root,
                "s-template-only",
                subagent_outbox="# Subagent Outbox\n\nplaceholder\n",
                mtime=CUTOFF - 1,
            )
            (template_only / "agents/subagents/worker/outbox.md").write_text(
                "# Subagent Outbox\n\n", encoding="utf-8"
            )
            active = ensure_session_layout(
                loop_root,
                "p-project",
                "s-active",
            )
            os.utime(active, (CUTOFF - DAY, CUTOFF - DAY))

            result = maintenance.maintain(loop_root, NOW)

            self.assertEqual(
                result,
                {
                    "operation": "maintain",
                    "warnings": [],
                    "deleted": [
                        {
                            "kind": "archived_session",
                            "id": "s-old-empty",
                            "path": str(old_empty),
                        },
                        {
                            "kind": "archived_session",
                            "id": "s-template-only",
                            "path": str(template_only),
                        },
                    ],
                    "preserved": [
                        {
                            "kind": "active_session",
                            "id": "s-active",
                            "path": str(active),
                            "reason": "active",
                        },
                        {
                            "kind": "archived_session",
                            "id": "s-boundary",
                            "path": str(boundary),
                            "reason": "retention",
                        },
                        {
                            "kind": "archived_session",
                            "id": "s-unresolved-main",
                            "path": str(unresolved_main),
                            "reason": "unresolved_outbox",
                        },
                        {
                            "kind": "archived_session",
                            "id": "s-unresolved-subagent",
                            "path": str(unresolved_subagent),
                            "reason": "unresolved_outbox",
                        },
                    ],
                },
            )
            self.assertFalse(old_empty.exists())
            self.assertFalse(template_only.exists())
            self.assertTrue(boundary.is_dir())
            self.assertTrue(unresolved_main.is_dir())
            self.assertTrue(unresolved_subagent.is_dir())
            self.assertTrue(active.is_dir())

    def test_complete_migration_cleanup_preserves_all_other_states_and_evidence(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            clean = self.complete_project_migration(temp, name="clean")
            boundary = self.complete_project_migration(
                temp,
                loop_root=clean["loop_root"],
                name="boundary",
            )
            loop_root = clean["loop_root"]
            os.utime(boundary["quarantine"], (CUTOFF, CUTOFF))
            os.utime(boundary["staging"], (CUTOFF, CUTOFF))
            ledger = loop_root / "migrations/ledger.jsonl"
            evidence_before = {
                "clean_manifest": clean["manifest_path"].read_bytes(),
                "boundary_manifest": boundary["manifest_path"].read_bytes(),
                "ledger": ledger.read_bytes(),
            }

            result = maintenance.maintain(loop_root, NOW)

            self.assertEqual(result["operation"], "maintain")
            self.assertEqual(
                {(item["kind"], item["id"]) for item in result["deleted"]},
                {
                    ("migration_quarantine", clean["manifest"]["migration_id"]),
                    ("migration_staging", clean["manifest"]["migration_id"]),
                },
            )
            self.assertEqual(
                {
                    (item["kind"], item["id"]): item["reason"]
                    for item in result["preserved"]
                },
                {
                    (
                        "migration_quarantine",
                        boundary["manifest"]["migration_id"],
                    ): "retention",
                    (
                        "migration_staging",
                        boundary["manifest"]["migration_id"],
                    ): "retention",
                },
            )
            self.assertEqual(result["warnings"], [])
            self.assertFalse(clean["quarantine"].exists())
            self.assertFalse(clean["staging"].exists())
            self.assertTrue(boundary["quarantine"].is_dir())
            self.assertTrue(boundary["staging"].is_dir())
            self.assertEqual(
                clean["manifest_path"].read_bytes(),
                evidence_before["clean_manifest"],
            )
            self.assertEqual(
                boundary["manifest_path"].read_bytes(),
                evidence_before["boundary_manifest"],
            )
            self.assertEqual(ledger.read_bytes(), evidence_before["ledger"])
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_missing_complete_migration_alias_blocks_all_maintenance_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fixture = self.complete_project_migration(temp)
            loop_root = fixture["loop_root"]
            archived = self.archive(
                loop_root,
                "s-unrelated-old",
                mtime=CUTOFF - DAY,
            )
            registry = loop_root / "registry.json"
            state = json.loads(registry.read_text(encoding="utf-8"))
            state["legacy_aliases"] = {}
            registry.write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            self.assert_loop_error(
                "maintenance_blocked",
                lambda: maintenance.maintain(loop_root, NOW),
            )

            self.assertTrue(archived.is_dir())
            self.assertTrue(fixture["quarantine"].is_dir())
            self.assertTrue(fixture["staging"].is_dir())

    def test_drifted_complete_migration_target_blocks_all_maintenance_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fixture = self.complete_project_migration(temp)
            loop_root = fixture["loop_root"]
            archived = self.archive(
                loop_root,
                "s-unrelated-old",
                mtime=CUTOFF - DAY,
            )
            target_file = fixture["target_file"]
            target_file.write_text(
                target_file.read_text(encoding="utf-8").replace(
                    fixture["imported"],
                    "- drifted replacement",
                ),
                encoding="utf-8",
            )

            self.assert_loop_error(
                "maintenance_blocked",
                lambda: maintenance.maintain(loop_root, NOW),
            )

            self.assertTrue(archived.is_dir())
            self.assertTrue(fixture["quarantine"].is_dir())
            self.assertTrue(fixture["staging"].is_dir())

    def test_drifted_quarantine_inventory_blocks_all_maintenance_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fixture = self.complete_project_migration(temp)
            loop_root = fixture["loop_root"]
            archived = self.archive(
                loop_root,
                "s-unrelated-old",
                mtime=CUTOFF - DAY,
            )
            quarantine_source = fixture["quarantine"] / "source"
            (quarantine_source / "unexpected.md").write_text(
                "unexpected inventory",
                encoding="utf-8",
            )

            self.assert_loop_error(
                "maintenance_blocked",
                lambda: maintenance.maintain(loop_root, NOW),
            )

            self.assertTrue(archived.is_dir())
            self.assertTrue(fixture["quarantine"].is_dir())
            self.assertTrue(fixture["staging"].is_dir())

    def test_invalid_migration_ledger_blocks_all_maintenance_writes(self):
        maintenance = self.maintenance_module()
        for case in ("missing", "corrupt", "inconsistent"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                fixture = self.complete_project_migration(Path(temp_dir))
                loop_root = fixture["loop_root"]
                archived = self.archive(
                    loop_root,
                    "s-unrelated-old",
                    mtime=CUTOFF - DAY,
                )
                ledger = loop_root / "migrations/ledger.jsonl"
                if case == "missing":
                    ledger.unlink()
                elif case == "corrupt":
                    ledger.write_text(SECRET, encoding="utf-8")
                else:
                    ledger.write_text(
                        json.dumps(
                            {
                                "migration_id": fixture["manifest"]["migration_id"],
                                "state": "complete",
                                "timestamp": NOW,
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )

                error = self.assert_loop_error(
                    "maintenance_blocked",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

                self.assertNotIn(SECRET, str(error))
                self.assertTrue(archived.is_dir())
                self.assertTrue(fixture["quarantine"].is_dir())
                self.assertTrue(fixture["staging"].is_dir())
                self.assertFalse(
                    (loop_root / "migrations/maintenance").exists()
                )

    def test_diagnose_reports_manifest_ledger_state_inconsistency_without_bodies(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fixture = self.complete_project_migration(temp)
            loop_root = fixture["loop_root"]
            migration_id = fixture["manifest"]["migration_id"]
            self.archive(
                loop_root,
                "s-secret-body",
                main_outbox=f"# Main Agent Outbox\n\n{SECRET}\n",
                mtime=CUTOFF - DAY,
            )
            ledger = loop_root / "migrations/ledger.jsonl"
            ledger.write_text(
                json.dumps(
                    {
                        "migration_id": migration_id,
                        "state": "complete",
                        "timestamp": NOW,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ledger_before = ledger.read_bytes()

            error = self.assert_loop_error(
                "maintenance_blocked",
                lambda: maintenance.maintain(loop_root, NOW),
            )
            diagnosis = maintenance.diagnose(loop_root, temp)

            self.assertEqual(ledger.read_bytes(), ledger_before)
            self.assertIn(
                {
                    "component": "migration_ledger",
                    "code": "corrupt_state",
                    "id": migration_id,
                },
                diagnosis["issues"],
            )
            self.assertNotIn(SECRET, str(error))
            self.assertNotIn(SECRET, json.dumps(diagnosis, sort_keys=True))

    def test_zero_manifest_ledger_state_blocks_and_is_diagnosed(self):
        maintenance = self.maintenance_module()
        ledger_only_id = "m-" + "f" * 32
        for case in ("malformed", "ledger_only"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                loop_root = temp / "loop"
                archived = self.archive(
                    loop_root,
                    "s-unrelated-old",
                    mtime=CUTOFF - DAY,
                )
                ledger = loop_root / "migrations/ledger.jsonl"
                ledger.parent.mkdir(parents=True)
                if case == "malformed":
                    ledger.write_text(SECRET, encoding="utf-8")
                    expected_issue = {
                        "component": "migration_ledger",
                        "code": "corrupt_state",
                    }
                else:
                    ledger.write_text(
                        json.dumps(
                            {
                                "migration_id": ledger_only_id,
                                "state": "detected",
                                "timestamp": 1,
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    expected_issue = {
                        "component": "migration_ledger",
                        "code": "ledger_only",
                        "id": ledger_only_id,
                    }
                ledger_before = ledger.read_bytes()

                error = self.assert_loop_error(
                    "maintenance_blocked",
                    lambda: maintenance.maintain(loop_root, NOW),
                )
                diagnosis = maintenance.diagnose(loop_root, temp)

                self.assertTrue(archived.is_dir())
                self.assertEqual(ledger.read_bytes(), ledger_before)
                self.assertIn(expected_issue, diagnosis["issues"])
                self.assertNotIn(SECRET, str(error))
                self.assertNotIn(SECRET, json.dumps(diagnosis, sort_keys=True))

    def test_maintenance_reads_ledger_once_for_multiple_manifests(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            first = self.complete_project_migration(temp, name="first")
            second = self.complete_project_migration(
                temp,
                loop_root=first["loop_root"],
                name="second",
            )
            real_read = maintenance.migration_module.read_ledger_events
            real_validate = maintenance.migration_module.validate_ledger_events

            with (
                mock.patch.object(
                    maintenance.migration_module,
                    "read_ledger_events",
                    wraps=real_read,
                ) as read_events,
                mock.patch.object(
                    maintenance.migration_module,
                    "validate_ledger_events",
                    wraps=real_validate,
                ) as validate_events,
            ):
                maintenance.maintain(first["loop_root"], NOW)

            self.assertEqual(read_events.call_count, 1)
            self.assertEqual(validate_events.call_count, 2)
            self.assertFalse(first["quarantine"].exists())
            self.assertFalse(second["quarantine"].exists())

    def test_nonterminal_or_conflicted_migration_blocks_all_deletion(self):
        maintenance = self.maintenance_module()
        for case in ("corrupt", "incomplete", "held", "conflicted"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                fixture = self.complete_project_migration(temp)
                loop_root = fixture["loop_root"]
                archived = self.archive(
                    loop_root,
                    "s-unrelated-old",
                    mtime=CUTOFF - DAY,
                )
                blocker_id = "m-" + {
                    "corrupt": "b",
                    "incomplete": "c",
                    "held": "d",
                    "conflicted": "e",
                }[case] * 32
                if case == "corrupt":
                    path = loop_root / f"migrations/manifests/{blocker_id}.json"
                    path.write_text(
                        json.dumps({"injected": SECRET}),
                        encoding="utf-8",
                    )
                elif case == "incomplete":
                    self.migration_directories(
                        loop_root,
                        blocker_id,
                        mtime=CUTOFF - DAY,
                    )
                    self.write_manifest(loop_root, blocker_id, "quarantined")
                elif case == "held":
                    self.migration_directories(
                        loop_root,
                        blocker_id,
                        mtime=CUTOFF - DAY,
                    )
                    self.write_manifest(
                        loop_root,
                        blocker_id,
                        "validated",
                        hold="governance_switch",
                    )
                else:
                    self.migration_directories(
                        loop_root,
                        blocker_id,
                        mtime=CUTOFF - DAY,
                    )

                error = self.assert_loop_error(
                    "maintenance_blocked",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

                self.assertNotIn(SECRET, str(error))
                self.assertTrue(archived.is_dir())
                self.assertTrue(fixture["quarantine"].is_dir())
                self.assertTrue(fixture["staging"].is_dir())

    def test_terminal_approved_protected_migration_is_retention_eligible(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(
                Path(temp_dir),
                protected=True,
            )
            manifest = fixture["manifest"]
            self.assertEqual(manifest["state"], "complete")
            self.assertIs(manifest["protected"], True)
            self.assertEqual(
                manifest["warnings"],
                ["Protected legacy source requires explicit approval."],
            )

            result = maintenance.maintain(fixture["loop_root"], NOW)

            self.assertFalse(fixture["quarantine"].exists())
            self.assertFalse(fixture["staging"].exists())
            self.assertEqual(
                {(item["kind"], item["id"]) for item in result["deleted"]},
                {
                    ("migration_quarantine", manifest["migration_id"]),
                    ("migration_staging", manifest["migration_id"]),
                },
            )

    def test_protected_complete_with_unresolved_warning_blocks_all_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(
                Path(temp_dir),
                protected=True,
            )
            loop_root = fixture["loop_root"]
            archived = self.archive(
                loop_root,
                "s-unrelated-old",
                mtime=CUTOFF - DAY,
            )
            manifest_path = fixture["manifest_path"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["warnings"].append("Unresolved migration conflict.")
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            self.assert_loop_error(
                "maintenance_blocked",
                lambda: maintenance.maintain(loop_root, NOW),
            )

            self.assertTrue(archived.is_dir())
            self.assertTrue(fixture["quarantine"].is_dir())
            self.assertTrue(fixture["staging"].is_dir())

    def test_partial_cleanup_retries_after_staging_deletion_failure(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            quarantine = fixture["quarantine"]
            staging = fixture["staging"]
            manifest_path = fixture["manifest_path"]
            ledger_path = loop_root / "migrations/ledger.jsonl"
            evidence_before = {
                "manifest": manifest_path.read_bytes(),
                "ledger": ledger_path.read_bytes(),
            }
            real_remove = maintenance._remove_tree
            failed = False

            def fail_staging_once(root, snapshot):
                nonlocal failed
                if snapshot.path == staging and not failed:
                    failed = True
                    return False
                return real_remove(root, snapshot)

            with mock.patch.object(
                maintenance,
                "_remove_tree",
                side_effect=fail_staging_once,
            ):
                first = maintenance.maintain(loop_root, NOW)

            self.assertFalse(quarantine.exists())
            self.assertTrue(staging.is_dir())
            self.assertIn(
                {
                    "kind": "migration_staging",
                    "id": fixture["manifest"]["migration_id"],
                    "path": str(staging),
                    "reason": "delete_failed",
                },
                first["preserved"],
            )
            self.assertEqual(manifest_path.read_bytes(), evidence_before["manifest"])
            self.assertEqual(ledger_path.read_bytes(), evidence_before["ledger"])

            second = maintenance.maintain(loop_root, NOW)

            self.assertFalse(staging.exists())
            self.assertIn(
                {
                    "kind": "migration_staging",
                    "id": fixture["manifest"]["migration_id"],
                    "path": str(staging),
                },
                second["deleted"],
            )
            self.assertEqual(manifest_path.read_bytes(), evidence_before["manifest"])
            self.assertEqual(ledger_path.read_bytes(), evidence_before["ledger"])

    def test_partial_recursive_quarantine_delete_is_resumable(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            quarantine = fixture["quarantine"]
            staging = fixture["staging"]
            manifest_before = fixture["manifest_path"].read_bytes()
            ledger = loop_root / "migrations/ledger.jsonl"
            ledger_before = ledger.read_bytes()
            quarantine_identity = (
                quarantine.stat().st_dev,
                quarantine.stat().st_ino,
            )
            real_rmtree = maintenance.shutil.rmtree
            interrupted = False

            def interrupt_quarantine_once(path):
                nonlocal interrupted
                if Path(path) == quarantine and not interrupted:
                    interrupted = True
                    (quarantine / "source/legacy.md").unlink()
                    raise OSError("simulated partial recursive deletion")
                return real_rmtree(path)

            with mock.patch.object(
                maintenance.shutil,
                "rmtree",
                side_effect=interrupt_quarantine_once,
            ):
                first = maintenance.maintain(loop_root, NOW)

            self.assertEqual(
                (quarantine.stat().st_dev, quarantine.stat().st_ino),
                quarantine_identity,
            )
            self.assertTrue(staging.is_dir())
            self.assertIn(
                {
                    "kind": "migration_quarantine",
                    "id": fixture["manifest"]["migration_id"],
                    "path": str(quarantine),
                    "reason": "delete_failed",
                },
                first["preserved"],
            )

            second = maintenance.maintain(loop_root, NOW)

            self.assertFalse(quarantine.exists())
            self.assertFalse(staging.exists())
            self.assertEqual(
                fixture["manifest_path"].read_bytes(),
                manifest_before,
            )
            self.assertEqual(ledger.read_bytes(), ledger_before)
            self.assertIn(
                {
                    "kind": "migration_quarantine",
                    "id": fixture["manifest"]["migration_id"],
                    "path": str(quarantine),
                },
                second["deleted"],
            )

    def test_partial_quarantine_cleanup_survives_valid_promotion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            quarantine = fixture["quarantine"]
            staging = fixture["staging"]
            real_rmtree = maintenance.shutil.rmtree
            interrupted = False

            def interrupt_quarantine_once(path):
                nonlocal interrupted
                if Path(path) == quarantine and not interrupted:
                    interrupted = True
                    (quarantine / "source/legacy.md").unlink()
                    raise OSError("simulated partial quarantine deletion")
                return real_rmtree(path)

            with mock.patch.object(
                maintenance.shutil,
                "rmtree",
                side_effect=interrupt_quarantine_once,
            ):
                maintenance.maintain(loop_root, NOW)

            changed = promote_entry(
                loop_root,
                fixture["manifest"]["project_id"],
                "project",
                "Verified Facts",
                "- [2026-08-10][verified] Promoted during cleanup recovery.\n"
                "  Evidence: partial quarantine recovery test\n",
            )
            result = maintenance.maintain(loop_root, NOW)

            self.assertTrue(changed)
            self.assertFalse(quarantine.exists())
            self.assertFalse(staging.exists())
            self.assertEqual(
                {(item["kind"], item["id"]) for item in result["deleted"]},
                {
                    (
                        "migration_quarantine",
                        fixture["manifest"]["migration_id"],
                    ),
                    (
                        "migration_staging",
                        fixture["manifest"]["migration_id"],
                    ),
                },
            )

    def test_live_promotion_lease_blocks_quarantine_cleanup(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            project_id = fixture["manifest"]["project_id"]
            lease_path = loop_root / f"locks/promote-project-{project_id}.lock"

            with FileLease(lease_path, "live-promotion"):
                self.assert_loop_error(
                    "lease_busy",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

            self.assertTrue(fixture["quarantine"].is_dir())
            self.assertTrue(fixture["staging"].is_dir())
            self.assertFalse(
                (loop_root / "migrations/maintenance").exists()
            )

    def test_partial_recursive_staging_delete_is_resumable(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            quarantine = fixture["quarantine"]
            staging = fixture["staging"]
            manifest_before = fixture["manifest_path"].read_bytes()
            ledger = loop_root / "migrations/ledger.jsonl"
            ledger_before = ledger.read_bytes()
            staging_identity = (staging.stat().st_dev, staging.stat().st_ino)
            real_rmtree = maintenance.shutil.rmtree
            interrupted = False

            def interrupt_staging_once(path):
                nonlocal interrupted
                if Path(path) == staging and not interrupted:
                    interrupted = True
                    (staging / "publish-plan.json").unlink()
                    raise OSError("simulated partial staging deletion")
                return real_rmtree(path)

            with mock.patch.object(
                maintenance.shutil,
                "rmtree",
                side_effect=interrupt_staging_once,
            ):
                first = maintenance.maintain(loop_root, NOW)

            self.assertFalse(quarantine.exists())
            self.assertEqual(
                (staging.stat().st_dev, staging.stat().st_ino),
                staging_identity,
            )
            self.assertFalse((staging / "publish-plan.json").exists())
            self.assertIn(
                {
                    "kind": "migration_staging",
                    "id": fixture["manifest"]["migration_id"],
                    "path": str(staging),
                    "reason": "delete_failed",
                },
                first["preserved"],
            )

            second = maintenance.maintain(loop_root, NOW)

            self.assertFalse(staging.exists())
            self.assertEqual(
                fixture["manifest_path"].read_bytes(),
                manifest_before,
            )
            self.assertEqual(ledger.read_bytes(), ledger_before)
            self.assertIn(
                {
                    "kind": "migration_staging",
                    "id": fixture["manifest"]["migration_id"],
                    "path": str(staging),
                },
                second["deleted"],
            )

    def test_complete_cleanup_marker_does_not_freeze_promoted_content(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            maintenance.maintain(loop_root, NOW)

            changed = promote_entry(
                loop_root,
                fixture["manifest"]["project_id"],
                "project",
                "Verified Facts",
                "- [2026-08-10][verified] Promoted after migration cleanup.\n"
                "  Evidence: terminal cleanup marker integration test\n",
            )
            result = maintenance.maintain(loop_root, NOW)

            self.assertTrue(changed)
            self.assertEqual(result["deleted"], [])
            self.assertEqual(result["warnings"], [])
            self.assertFalse(fixture["quarantine"].exists())
            self.assertFalse(fixture["staging"].exists())

    def test_complete_cleanup_marker_is_compact_and_idempotent(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            migration_id = fixture["manifest"]["migration_id"]

            maintenance.maintain(loop_root, NOW)
            marker_path = (
                loop_root / f"migrations/maintenance/{migration_id}.json"
            )
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker_before = marker_path.read_bytes()
            second = maintenance.maintain(loop_root, NOW)

            self.assertEqual(
                set(marker),
                {
                    "schema_version",
                    "migration_id",
                    "manifest_sha256",
                    "manifest_identity",
                    "phase",
                },
            )
            self.assertEqual(marker["phase"], "complete")
            self.assertEqual(marker_path.read_bytes(), marker_before)
            self.assertEqual(second["deleted"], [])
            self.assertEqual(second["warnings"], [])

    def test_symlink_preflight_is_fail_closed_before_any_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            unsafe = self.archive(
                loop_root,
                "s-unsafe",
                mtime=CUTOFF - DAY,
            )
            safe = self.archive(
                loop_root,
                "s-safe",
                mtime=CUTOFF - DAY,
            )
            outside = temp / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            (unsafe / "outside-link").symlink_to(outside, target_is_directory=True)

            self.assert_loop_error(
                "unsafe_path",
                lambda: maintenance.maintain(loop_root, NOW),
            )

            self.assertTrue(safe.is_dir())
            self.assertTrue(unsafe.is_dir())
            self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_outbox_read_failure_preserves_the_archived_session(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            archived = self.archive(
                loop_root,
                "s-unreadable-outbox",
                mtime=CUTOFF - DAY,
            )
            outbox = archived / "agents/main/outbox.md"
            outbox_identity = (outbox.stat().st_dev, outbox.stat().st_ino)
            real_read = maintenance.os.read

            def fail_outbox_read(descriptor, size):
                opened = maintenance.os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) == outbox_identity:
                    raise PermissionError("simulated unreadable outbox")
                return real_read(descriptor, size)

            with mock.patch.object(
                maintenance.os,
                "read",
                side_effect=fail_outbox_read,
            ):
                result = maintenance.maintain(loop_root, NOW)

            self.assertTrue(archived.is_dir())
            self.assertEqual(
                result["preserved"],
                [
                    {
                        "kind": "archived_session",
                        "id": "s-unreadable-outbox",
                        "path": str(archived),
                        "reason": "unresolved_outbox",
                    }
                ],
            )

    def test_large_non_template_outbox_is_rejected_with_bounded_early_read(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            archived = self.archive(
                loop_root,
                "s-large-outbox",
                mtime=CUTOFF - DAY,
            )
            outbox = archived / "agents/main/outbox.md"
            outbox.write_bytes(
                SECRET.encode("ascii") + b"x" * (2 * 1024 * 1024)
            )
            outbox_identity = (outbox.stat().st_dev, outbox.stat().st_ino)
            real_read = maintenance.os.read
            outbox_reads: list[int] = []

            def track_outbox_reads(descriptor, size):
                opened = maintenance.os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) == outbox_identity:
                    outbox_reads.append(size)
                return real_read(descriptor, size)

            with mock.patch.object(
                maintenance.os,
                "read",
                side_effect=track_outbox_reads,
            ):
                result = maintenance.maintain(loop_root, NOW)

            self.assertTrue(archived.is_dir())
            self.assertEqual(outbox_reads, [4096])
            self.assertEqual(result["preserved"][0]["reason"], "unresolved_outbox")
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_missing_main_outbox_is_treated_as_empty_for_archived_cleanup(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            archived = self.archive(
                loop_root,
                "s-missing-main-outbox",
                mtime=CUTOFF - DAY,
            )
            (archived / "agents/main/outbox.md").unlink()
            os.utime(archived, (CUTOFF - DAY, CUTOFF - DAY))

            result = maintenance.maintain(loop_root, NOW)

            self.assertFalse(archived.exists())
            self.assertEqual(result["preserved"], [])
            self.assertEqual(result["deleted"][0]["id"], "s-missing-main-outbox")

    def test_missing_discovered_subagent_outbox_is_treated_as_empty(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            archived = self.archive(
                loop_root,
                "s-missing-subagent-outbox",
                mtime=CUTOFF - DAY,
            )
            worker = archived / "agents/subagents/worker"
            worker.mkdir()
            (worker / "inbox.md").write_text(
                "# Subagent Inbox\n",
                encoding="utf-8",
            )
            os.utime(archived, (CUTOFF - DAY, CUTOFF - DAY))

            result = maintenance.maintain(loop_root, NOW)

            self.assertFalse(archived.exists())
            self.assertEqual(result["preserved"], [])
            self.assertEqual(result["deleted"][0]["id"], "s-missing-subagent-outbox")

    def test_manifest_swap_to_symlink_is_rejected_without_following_it(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            ensure_project_layout(loop_root, "p-project")
            RegistryStore(loop_root).initialize()
            migration_id = "m-" + "a" * 32
            quarantine, staging = self.migration_directories(
                loop_root,
                migration_id,
                mtime=CUTOFF - DAY,
            )
            manifest = self.write_manifest(
                loop_root,
                migration_id,
                "complete",
            ).resolve()
            outside = temp / "outside-manifest.json"
            outside.write_bytes(manifest.read_bytes())
            backup = manifest.with_suffix(".backup")
            original_lstat = Path.lstat
            manifest_lstats = 0

            def racing_lstat(path):
                nonlocal manifest_lstats
                value = original_lstat(path)
                if path == manifest:
                    manifest_lstats += 1
                    if manifest_lstats == 2:
                        manifest.rename(backup)
                        manifest.symlink_to(outside)
                return value

            with mock.patch.object(Path, "lstat", racing_lstat):
                self.assert_loop_error(
                    "unsafe_path",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

            self.assertTrue(quarantine.is_dir())
            self.assertTrue(staging.is_dir())
            self.assertTrue(outside.is_file())

    def test_root_symlink_reserved_root_and_live_maintenance_lease_are_rejected(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            ensure_project_layout(loop_root, "p-project")
            alias = temp / "loop-alias"
            alias.symlink_to(loop_root, target_is_directory=True)
            self.assert_loop_error(
                "unsafe_path",
                lambda: maintenance.maintain(alias, NOW),
            )

            with mock.patch.object(
                maintenance,
                "is_reserved_product_path",
                return_value=True,
            ):
                self.assert_loop_error(
                    "reserved_product_memory",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

            lease_path = loop_root / "locks/maintenance.lock"
            with FileLease(lease_path, "other-maintainer"):
                self.assert_loop_error(
                    "lease_busy",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

    def test_product_root_maintenance_and_diagnosis_are_filesystem_opaque(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            codex_home = home / ".codex"
            product_root = codex_home / "memories"
            cwd = temp / "project"
            cwd.mkdir()
            original_resolve = Path.resolve
            original_lstat = Path.lstat
            original_stat = Path.stat

            def under_product(path: Path) -> bool:
                lexical = Path(os.path.abspath(path))
                try:
                    lexical.relative_to(codex_home)
                    return True
                except ValueError:
                    return False

            def guarded_resolve(path, *args, **kwargs):
                if under_product(path):
                    raise AssertionError("product path was resolved")
                return original_resolve(path, *args, **kwargs)

            def guarded_lstat(path, *args, **kwargs):
                if under_product(path):
                    raise AssertionError("product path was lstatted")
                return original_lstat(path, *args, **kwargs)

            def guarded_stat(path, *args, **kwargs):
                if under_product(path):
                    raise AssertionError("product path was statted")
                return original_stat(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(Path, "resolve", new=guarded_resolve),
                mock.patch.object(Path, "lstat", new=guarded_lstat),
                mock.patch.object(Path, "stat", new=guarded_stat),
            ):
                self.assert_loop_error(
                    "reserved_product_memory",
                    lambda: maintenance.maintain(product_root, NOW),
                )
                diagnosis = maintenance.diagnose(product_root, cwd)

            self.assertIn(
                {"component": "root", "code": "reserved_product_memory"},
                diagnosis["issues"],
            )
            self.assertFalse(diagnosis["root"]["exists"])
            self.assertNotIn("mode_0700", diagnosis["root"])

    def test_live_migration_lease_blocks_all_maintenance_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            archived = self.archive(
                loop_root,
                "s-old",
                mtime=CUTOFF - DAY,
            )
            migration_lease = loop_root / "locks/migration.lock"

            with FileLease(migration_lease, "live-migration"):
                self.assert_loop_error(
                    "lease_busy",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

            self.assertTrue(archived.is_dir())

    def test_live_archive_lease_is_acquired_before_any_session_deletion(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            unlocked = self.archive(
                loop_root,
                "s-a-unlocked",
                mtime=CUTOFF - DAY,
            )
            locked = self.archive(
                loop_root,
                "s-z-locked",
                mtime=CUTOFF - DAY,
            )
            archive_lease = (
                loop_root / "locks/archive-p-project-s-z-locked.lock"
            )

            with FileLease(archive_lease, "live-archive"):
                self.assert_loop_error(
                    "lease_busy",
                    lambda: maintenance.maintain(loop_root, NOW),
                )

            self.assertTrue(unlocked.is_dir())
            self.assertTrue(locked.is_dir())

    def test_diagnose_returns_only_metadata_and_does_not_read_memory_bodies(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            discovery = discover_project(cwd)
            store = RegistryStore(loop_root, id_factory=lambda: "project")
            store.initialize()
            project_id = store.resolve_project(discovery, create=True)
            self.assertEqual(project_id, "p-project")
            project = ensure_project_layout(loop_root, project_id)
            session = ensure_session_layout(loop_root, project_id, "s-session")
            (loop_root / "global/long.md").write_text(SECRET, encoding="utf-8")
            (project / "project.md").write_text(SECRET, encoding="utf-8")
            (session / "status.md").write_text(SECRET, encoding="utf-8")
            (session / "agents/main/outbox.md").write_text(
                SECRET,
                encoding="utf-8",
            )

            held_id = "m-" + "7" * 32
            self.write_manifest(
                loop_root,
                held_id,
                "validated",
                hold="governance_switch",
            )
            corrupt_id = "m-" + "8" * 32
            corrupt = loop_root / f"migrations/manifests/{corrupt_id}.json"
            corrupt.write_text(
                json.dumps({"injected": SECRET}),
                encoding="utf-8",
            )

            lease_path = loop_root / "locks/work.lock"
            with FileLease(lease_path, "worker"):
                before_files = sorted(
                    path.relative_to(loop_root).as_posix()
                    for path in loop_root.rglob("*")
                )
                result = maintenance.diagnose(loop_root, cwd)
                after_files = sorted(
                    path.relative_to(loop_root).as_posix()
                    for path in loop_root.rglob("*")
                )

            self.assertEqual(before_files, after_files)
            self.assertEqual(
                set(result),
                {
                    "operation",
                    "root",
                    "registry",
                    "active_locks",
                    "stale_locks",
                    "incomplete_migrations",
                    "discovery",
                    "containment",
                    "issues",
                },
            )
            self.assertEqual(result["operation"], "diagnose")
            self.assertEqual(
                result["root"],
                {
                    "path": str(loop_root.resolve()),
                    "exists": True,
                    "is_directory": True,
                    "is_symlink": False,
                    "owner_uid": os.getuid(),
                    "expected_owner_uid": os.getuid(),
                    "owned_by_current_user": True,
                    "mode": f"{stat.S_IMODE(loop_root.stat().st_mode):04o}",
                },
            )
            self.assertEqual(
                result["registry"],
                {
                    "exists": True,
                    "schema_version": 1,
                    "integrity": "ok",
                },
            )
            self.assertEqual(result["active_locks"], ["work.lock"])
            self.assertEqual(result["stale_locks"], [])
            self.assertEqual(
                result["incomplete_migrations"],
                [
                    {
                        "migration_id": held_id,
                        "state": "validated",
                        "hold": "governance_switch",
                    }
                ],
            )
            self.assertEqual(result["discovery"]["project_id"], project_id)
            self.assertEqual(result["discovery"]["kind"], "directory")
            self.assertIsNone(result["discovery"]["alias"])
            self.assertTrue(result["containment"]["all"])
            self.assertEqual(
                result["issues"],
                [
                    {
                        "component": "migration_ledger",
                        "code": "corrupt_state",
                        "id": held_id,
                    },
                    {
                        "component": "migration_manifest",
                        "code": "corrupt_state",
                        "id": corrupt_id,
                    }
                ],
            )
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_diagnose_missing_root_does_not_initialize_or_repair_it(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "missing-loop"
            cwd = temp / "project"
            cwd.mkdir()

            result = maintenance.diagnose(loop_root, cwd)

            self.assertFalse(loop_root.exists())
            self.assertFalse(result["root"]["exists"])
            self.assertEqual(result["registry"]["integrity"], "missing")
            self.assertEqual(result["active_locks"], [])
            self.assertEqual(result["stale_locks"], [])
            self.assertEqual(result["incomplete_migrations"], [])
            self.assertIsNone(result["discovery"]["project_id"])
            self.assertIn(
                {"component": "root", "code": "missing"},
                result["issues"],
            )

    def test_diagnose_reports_only_semantically_active_leases(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            RegistryStore(loop_root).initialize()
            locks = loop_root / "locks"

            def write_lease(name: str, pid: int, expires_at: float) -> None:
                (locks / name).write_text(
                    json.dumps(
                        {
                            "owner": SECRET,
                            "pid": pid,
                            "acquired_at": 10,
                            "expires_at": expires_at,
                            "token": SECRET,
                        }
                    ),
                    encoding="utf-8",
                )

            write_lease("future.lock", 111, 110)
            write_lease("live-pid.lock", 222, 90)
            write_lease("stale.lock", 333, 90)
            (locks / "malformed.lock").write_text(SECRET, encoding="utf-8")
            outside = temp / "outside.lock"
            outside.write_text(SECRET, encoding="utf-8")
            (locks / "symlink.lock").symlink_to(outside)
            os.mkfifo(locks / "special.lock")

            with (
                mock.patch.object(maintenance.time, "time", return_value=100),
                mock.patch.object(
                    maintenance.storage_module,
                    "pid_is_alive",
                    side_effect=lambda pid: pid == 222,
                ),
            ):
                result = maintenance.diagnose(loop_root, cwd)

            self.assertEqual(
                result["active_locks"],
                ["future.lock", "live-pid.lock"],
            )
            self.assertEqual(result["stale_locks"], ["stale.lock"])
            self.assertIn(
                {
                    "component": "locks",
                    "code": "corrupt_state",
                    "id": "malformed.lock",
                },
                result["issues"],
            )
            for name in ("symlink.lock", "special.lock"):
                self.assertIn(
                    {
                        "component": "locks",
                        "code": "unsafe_path",
                        "id": name,
                    },
                    result["issues"],
                )
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_diagnose_distinguishes_stale_leases_from_absent(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            RegistryStore(loop_root).initialize()
            stale = loop_root / "locks/stale.lock"
            stale.write_text(
                json.dumps(
                    {
                        "owner": SECRET,
                        "pid": 999,
                        "acquired_at": 10,
                        "expires_at": 90,
                        "token": SECRET,
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(maintenance.time, "time", return_value=100),
                mock.patch.object(
                    maintenance.storage_module,
                    "pid_is_alive",
                    return_value=False,
                ),
            ):
                stale_result = maintenance.diagnose(loop_root, cwd)
                self.assertTrue(stale.is_file())
                stale.unlink()
                absent_result = maintenance.diagnose(loop_root, cwd)

            self.assertEqual(stale_result["active_locks"], [])
            self.assertEqual(stale_result["stale_locks"], ["stale.lock"])
            self.assertEqual(absent_result["stale_locks"], [])
            self.assertNotIn(SECRET, json.dumps(stale_result, sort_keys=True))

    def test_diagnose_reports_corrupt_cleanup_marker_without_leaking_it(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            RegistryStore(loop_root).initialize()
            migration_id = "m-" + "a" * 32
            marker_dir = loop_root / "migrations/maintenance"
            marker_dir.mkdir(parents=True)
            marker = marker_dir / f"{migration_id}.json"
            marker.write_text(
                json.dumps({"injected": SECRET}),
                encoding="utf-8",
            )

            result = maintenance.diagnose(loop_root, cwd)

            self.assertIn(
                {
                    "component": "migration_cleanup",
                    "code": "corrupt_state",
                    "id": migration_id,
                },
                result["issues"],
            )
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_unhashable_cleanup_phase_blocks_and_is_diagnosed_as_corruption(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fixture = self.complete_project_migration(temp)
            loop_root = fixture["loop_root"]
            migration_id = fixture["manifest"]["migration_id"]
            maintenance.maintain(loop_root, NOW)
            marker_path = (
                loop_root / f"migrations/maintenance/{migration_id}.json"
            )
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["phase"] = []
            marker["injected"] = SECRET
            marker_path.write_text(
                json.dumps(marker, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            marker_before = marker_path.read_bytes()

            error = self.assert_loop_error(
                "maintenance_blocked",
                lambda: maintenance.maintain(loop_root, NOW),
            )
            diagnosis = maintenance.diagnose(loop_root, temp)

            self.assertEqual(marker_path.read_bytes(), marker_before)
            self.assertIn(
                {
                    "component": "migration_cleanup",
                    "code": "corrupt_state",
                    "id": migration_id,
                },
                diagnosis["issues"],
            )
            self.assertNotIn(SECRET, str(error))
            self.assertNotIn(SECRET, json.dumps(diagnosis, sort_keys=True))

    def test_diagnose_reports_partial_cleanup_phase_without_reading_body(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.complete_project_migration(Path(temp_dir))
            loop_root = fixture["loop_root"]
            quarantine = fixture["quarantine"]
            real_rmtree = maintenance.shutil.rmtree
            interrupted = False

            def interrupt_quarantine_once(path):
                nonlocal interrupted
                if Path(path) == quarantine and not interrupted:
                    interrupted = True
                    (quarantine / "source/legacy.md").unlink()
                    raise OSError("simulated partial quarantine deletion")
                return real_rmtree(path)

            with mock.patch.object(
                maintenance.shutil,
                "rmtree",
                side_effect=interrupt_quarantine_once,
            ):
                maintenance.maintain(loop_root, NOW)
            fixture["target_file"].write_text(SECRET, encoding="utf-8")

            result = maintenance.diagnose(loop_root, Path(temp_dir))

            self.assertIn(
                {
                    "migration_id": fixture["manifest"]["migration_id"],
                    "state": "cleanup:quarantine_deleting",
                    "hold": None,
                },
                result["incomplete_migrations"],
            )
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_diagnose_reports_manifest_path_escape_without_leaking_values(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            RegistryStore(loop_root).initialize()
            migration_id = "m-" + "9" * 32
            outside_staging = (
                temp / "outside/migrations/staging" / migration_id
            ).resolve()
            manifest_path = self.write_manifest(
                loop_root,
                migration_id,
                "validated",
                hold="governance_switch",
                staging_path=outside_staging,
            )
            manifest_before = manifest_path.read_bytes()

            result = maintenance.diagnose(loop_root, cwd)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertFalse(result["containment"]["migrations"])
            self.assertFalse(result["containment"]["all"])
            self.assertIn(
                {
                    "component": "migration_manifest",
                    "code": "path_outside_loop_root",
                    "id": migration_id,
                },
                result["issues"],
            )
            self.assertNotIn(SECRET, json.dumps(result, sort_keys=True))

    def test_diagnose_registry_swap_to_symlink_is_a_structured_issue(self):
        maintenance = self.maintenance_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            cwd = temp / "project"
            cwd.mkdir()
            RegistryStore(loop_root).initialize()
            registry = (loop_root / "registry.json").resolve()
            shadow = loop_root / "registry-shadow.json"
            shadow.write_bytes(registry.read_bytes())
            backup = registry.with_suffix(".backup")
            original_lstat = Path.lstat
            swapped = False

            def racing_lstat(path):
                nonlocal swapped
                value = original_lstat(path)
                if path == registry and not swapped:
                    swapped = True
                    registry.rename(backup)
                    registry.symlink_to(shadow)
                return value

            with mock.patch.object(Path, "lstat", racing_lstat):
                result = maintenance.diagnose(loop_root, cwd)

            self.assertEqual(result["registry"]["integrity"], "corrupt")
            self.assertIn(
                {"component": "registry", "code": "unsafe_path"},
                result["issues"],
            )
            self.assertIsNone(result["discovery"]["project_id"])
            self.assertTrue(shadow.is_file())


class CliMigrationOrchestrationEndToEndTests(unittest.TestCase):
    def run_cli(self, *arguments: object) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *(str(value) for value in arguments)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        return completed, payload

    def scan_empty_migrations(
        self,
        temp: Path,
        *,
        count: int = 2,
    ) -> tuple[Path, Path, list[Path], list[Path]]:
        loop_root = temp / "loop"
        cwd = temp / "project"
        cwd.mkdir()
        sources: list[Path] = []
        arguments: list[object] = ["migrate-scan", "--cwd", cwd]
        for index in range(count):
            source = temp / f"legacy-{index}" / ".memory"
            source.mkdir(parents=True)
            sources.append(source)
            arguments.extend(("--legacy-path", source))
        arguments.extend(("--root", loop_root, "--json"))

        completed, payload = self.run_cli(*arguments)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifests = [Path(value) for value in payload["manifests"]]
        self.assertEqual(len(manifests), count)
        return loop_root, cwd, sources, manifests

    def empty_classification(self, temp: Path, manifest_path: Path) -> Path:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path = temp / f"{manifest['migration_id']}-classification.json"
        path.write_text(
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
        return path

    def test_cli_apply_rebinds_missing_manifest_snapshot_before_namespace_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, _, manifests = self.scan_empty_migrations(temp, count=1)
            manifest_path = manifests[0]
            classification = self.empty_classification(temp, manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_snapshot = manifest["snapshot"]
            manifest["snapshot"] = (
                f"migrations/quarantine/{manifest['migration_id']}/source"
            )
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                manifest_path,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["migration"]["state"], "complete")
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["snapshot"], expected_snapshot)

    def test_cli_apply_does_not_recover_unselected_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, _, manifests = self.scan_empty_migrations(temp)
            selected, unrelated_path = manifests
            classification = self.empty_classification(temp, selected)
            unrelated = json.loads(unrelated_path.read_text(encoding="utf-8"))
            unrelated["snapshot"] = (
                f"migrations/quarantine/{unrelated['migration_id']}/source"
            )
            unrelated_path.write_text(
                json.dumps(unrelated, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            unrelated_before = unrelated_path.read_bytes()

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                selected,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["migration"]["state"], "complete")
            self.assertEqual(unrelated_path.read_bytes(), unrelated_before)

    def test_inventoried_external_drift_does_not_replace_snapshot_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            legacy_file = source / "legacy.md"
            legacy_file.write_text("# Legacy\n\nOriginal body.\n", encoding="utf-8")

            scanned, scan_payload = self.run_cli(
                "migrate-scan",
                "--cwd",
                cwd,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            manifest_path = Path(scan_payload["manifests"][0]).resolve()
            legacy_file.write_text(
                f"# Legacy\n\nChanged body.\n\n{SECRET}\n",
                encoding="utf-8",
            )
            inventoried = migration_module.load_manifest(manifest_path)

            preflight, preflight_payload = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-refresh-required",
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertTrue(preflight_payload["ok"])
            self.assertNotIn(SECRET, preflight.stdout)
            self.assertNotIn(SECRET, preflight.stderr)
            converged = migration_module.load_manifest(manifest_path)
            self.assertEqual(converged["migration_id"], inventoried["migration_id"])
            self.assertEqual(converged["state"], inventoried["state"])
            self.assertEqual(converged["source_inventory_sha256"], inventoried["source_inventory_sha256"])

    def test_copied_drift_is_not_translated_to_refresh_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            scanned, scan_payload = self.run_cli(
                "migrate-scan",
                "--cwd",
                cwd,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            manifest_path = Path(scan_payload["manifests"][0])
            classification = self.empty_classification(temp, manifest_path)
            original_ledger_event = migration_module._ensure_ledger_event
            interrupted = False

            def interrupt_after_copied(root, migration_id, state):
                nonlocal interrupted
                if state == "copied" and not interrupted:
                    interrupted = True
                    raise RuntimeError("interrupt after copied manifest")
                return original_ledger_event(root, migration_id, state)

            with mock.patch.object(
                migration_module,
                "_ensure_ledger_event",
                side_effect=interrupt_after_copied,
            ):
                with self.assertRaisesRegex(RuntimeError, "after copied manifest"):
                    migration_module.apply_migration(
                        loop_root,
                        manifest_path,
                        classification,
                    )
            self.assertEqual(
                migration_module.load_manifest(manifest_path)["state"],
                "copied",
            )
            (source / "late.md").write_text(SECRET, encoding="utf-8")

            blocked, payload = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-copied-drift",
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["degraded"])
            self.assertFalse(payload["capabilities"]["project_promote"])
            self.assertTrue(payload["capabilities"]["session_write"])
            self.assertNotIn(SECRET, blocked.stdout)
            self.assertNotIn(SECRET, blocked.stderr)

    def test_null_later_state_metadata_drift_never_becomes_internal_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            legacy_file = source / "project.md"
            legacy_file.write_text("# Legacy\n", encoding="utf-8")
            scanned, scan_payload = self.run_cli(
                "migrate-scan",
                "--cwd",
                cwd,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            manifest_path = Path(scan_payload["manifests"][0])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["target_files"] = None
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            legacy_file.write_text("# Changed legacy\n", encoding="utf-8")

            blocked, payload = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-null-target-files",
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["degraded"])
            self.assertFalse(payload["capabilities"]["project_promote"])
            self.assertTrue(payload["capabilities"]["session_write"])

    def test_source_kind_drift_is_not_advertised_as_refreshable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            scanned, _ = self.run_cli(
                "migrate-scan",
                "--cwd",
                cwd,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            (source / "project.md").write_text(
                "# Material source identity change\n",
                encoding="utf-8",
            )

            blocked, payload = self.run_cli(
                "preflight",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-source-kind-drift",
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertTrue(payload["ok"])

    def test_apply_blocks_on_unrelated_manifest_namespace_garbage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, sources, manifests = self.scan_empty_migrations(temp)
            intended = manifests[0]
            before = intended.read_bytes()
            classification = self.empty_classification(temp, intended)
            garbage = intended.parent / "garbage.tmp"
            garbage.write_text("unexpected metadata", encoding="utf-8")

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                intended,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 4, completed.stderr)
            self.assertEqual(payload["error"]["code"], "corrupt_state")
            self.assertEqual(intended.read_bytes(), before)
            self.assertTrue(all(source.is_dir() for source in sources))

    def test_invalid_classification_blocks_before_namespace_recovery_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, sources, manifests = self.scan_empty_migrations(temp)
            intended = manifests[0]
            unrelated = json.loads(manifests[1].read_text(encoding="utf-8"))
            ledger = loop_root / "migrations" / "ledger.jsonl"
            events = [json.loads(line) for line in ledger.read_text().splitlines()]
            ledger.write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in events
                    if event["migration_id"] != unrelated["migration_id"]
                ),
                encoding="utf-8",
            )
            classification = temp / "invalid-classification.json"
            classification.write_text(
                f'{{"injected":"{SECRET}",',
                encoding="utf-8",
            )
            registry = loop_root / "registry.json"
            before = {
                "registry": registry.read_bytes(),
                "ledger": ledger.read_bytes(),
                "manifests": {path: path.read_bytes() for path in manifests},
            }

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                intended,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 4, completed.stderr)
            self.assertEqual(payload["error"]["code"], "invalid_classification")
            self.assertEqual(registry.read_bytes(), before["registry"])
            self.assertEqual(ledger.read_bytes(), before["ledger"])
            for path, content in before["manifests"].items():
                self.assertEqual(path.read_bytes(), content)
            self.assertTrue(all(source.is_dir() for source in sources))
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)

    def test_cli_reports_unhashable_manifest_state_as_typed_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, sources, manifests = self.scan_empty_migrations(
                temp,
                count=1,
            )
            manifest_path = manifests[0]
            classification = self.empty_classification(temp, manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = []
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            before = manifest_path.read_bytes()

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                manifest_path,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 4, completed.stderr)
            self.assertEqual(payload["error"]["code"], "corrupt_state")
            self.assertEqual(manifest_path.read_bytes(), before)
            self.assertTrue(sources[0].is_dir())

    def test_apply_uses_snapshots_when_unrelated_external_source_is_unsafe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, sources, manifests = self.scan_empty_migrations(temp)
            intended = manifests[0]
            before = intended.read_bytes()
            classification = self.empty_classification(temp, intended)
            unsafe_source = sources[1]
            unsafe_source.rmdir()
            outside = temp / "outside"
            outside.mkdir()
            unsafe_source.symlink_to(outside, target_is_directory=True)

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                intended,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["migration"]["state"], "complete")
            self.assertNotEqual(intended.read_bytes(), before)
            self.assertTrue(sources[0].is_dir())

    def test_apply_allows_one_of_multiple_clean_pending_migrations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root, _, sources, manifests = self.scan_empty_migrations(temp)
            intended = manifests[0]
            unrelated = manifests[1]
            classification = self.empty_classification(temp, intended)

            completed, payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                intended,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["migration"]["state"], "complete")
            self.assertEqual(
                json.loads(unrelated.read_text(encoding="utf-8"))["state"],
                "inventoried",
            )
            self.assertTrue(sources[0].is_dir())
            self.assertTrue(sources[1].is_dir())

    def test_unrelated_pending_migration_allows_writes_and_scan_apply_can_manage_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            cwd = temp / "project"
            legacy = temp / "legacy" / ".memory"
            cwd.mkdir()
            legacy.mkdir(parents=True)
            status_input = temp / "status.md"
            status_input.write_text(
                f"# Session Status\n\n{SECRET}\n",
                encoding="utf-8",
            )

            scanned, scan_payload = self.run_cli(
                "migrate-scan",
                "--cwd",
                cwd,
                "--legacy-path",
                legacy,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            manifest_path = Path(scan_payload["manifests"][0])

            rescanned, rescan_payload = self.run_cli(
                "migrate-scan",
                "--cwd",
                cwd,
                "--legacy-path",
                legacy,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(rescanned.returncode, 0, rescanned.stderr)
            self.assertEqual(rescan_payload["manifests"], [str(manifest_path)])

            written, write_payload = self.run_cli(
                "session-write",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-pending",
                "--kind",
                "status",
                "--input",
                status_input,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            destination = Path(write_payload["path"])
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                status_input.read_text(encoding="utf-8"),
            )
            self.assertNotIn(SECRET, written.stdout)
            self.assertNotIn(SECRET, written.stderr)

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
            applied, apply_payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                manifest_path,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(apply_payload["migration"]["state"], "complete")

            rewritten, rewrite_payload = self.run_cli(
                "session-write",
                "--cwd",
                cwd,
                "--thread-id",
                "thread-pending",
                "--kind",
                "status",
                "--input",
                status_input,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(rewritten.returncode, 0, rewritten.stderr)
            self.assertEqual(Path(rewrite_payload["path"]), destination)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                status_input.read_text(encoding="utf-8"),
            )
            self.assertNotIn(SECRET, rewritten.stdout)
            self.assertNotIn(SECRET, rewritten.stderr)

    def test_completed_legacy_alias_resolves_target_without_recreating_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            stale_project = temp / "stale-project"
            legacy = stale_project / ".memory"
            legacy.mkdir(parents=True)

            scanned, scan_payload = self.run_cli(
                "migrate-scan",
                "--cwd",
                stale_project,
                "--legacy-path",
                legacy,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            manifest_path = Path(scan_payload["manifests"][0])
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
            applied, apply_payload = self.run_cli(
                "migrate-apply",
                "--manifest",
                manifest_path,
                "--classification",
                classification,
                "--root",
                loop_root,
                "--json",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            target = Path(apply_payload["migration"]["target"])
            self.assertTrue(legacy.is_dir())

            completed, payload = self.run_cli(
                "preflight",
                "--cwd",
                stale_project,
                "--thread-id",
                "thread-stale-alias",
                "--root",
                loop_root,
                "--json",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(Path(payload["paths"]["project"]), target)
            self.assertEqual(payload["project_id"], manifest["project_id"])
            self.assertTrue(legacy.is_dir())


if __name__ == "__main__":
    unittest.main()
