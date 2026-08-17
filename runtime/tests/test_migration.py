import hashlib
import importlib
import json
import os
from contextlib import ExitStack, contextmanager
from dataclasses import FrozenInstanceError, replace
import errno
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import global_facts, legacy, migration
from scripts.loopmem.registry import RegistryStore
from scripts.loopmem.sessions import PROJECT_SECTIONS, promote_entry
from scripts.loopmem.storage import FileLease


PROJECT_TEMPLATE = (
    "# Project Memory\n\n"
    + "\n\n".join(f"## {section}" for section in PROJECT_SECTIONS)
    + "\n"
)


class MigrationTests(unittest.TestCase):
    def test_global_long_receipt_covers_only_exact_archived_entry_hashes(self):
        legacy_long = (
            "# Global Long-Term Memory\n\n## Entries\n\n"
            "- [2026-08-14][verified] Archived global fact.\n"
            "  Evidence: migration receipt test\n"
        )
        methodology = (
            "# Global Long-Term Memory\n\n## Methodology\n\n"
            "## Fact Index\n\n"
            "- `~/loop-memory/global/facts/index.md`\n"
        )
        block = (
            "- [2026-08-14][verified] Archived global fact.\n"
            "  Evidence: migration receipt test"
        )
        required = hashlib.sha256(block.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop"
            global_facts.ensure_facts_layout(root)
            (root / "global/long.md").write_text(legacy_long, encoding="utf-8")
            result = global_facts.organize_global_long(root, methodology)

            self.assertTrue(global_facts.verify_receipt_coverage(root, {required}))
            self.assertFalse(
                global_facts.verify_receipt_coverage(root, {"0" * 64})
            )

            Path(result["history"]).write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(LoopMemoryError) as context:
                global_facts.verify_receipt_coverage(root, {required})
            self.assertEqual(context.exception.code, "global_receipt_invalid")

    @contextmanager
    def deny_external_source_io(self, source: Path):
        source = Path(os.path.abspath(source))
        original_lstat = Path.lstat
        original_read_bytes = Path.read_bytes
        original_open = os.open

        def is_external(candidate) -> bool:
            try:
                Path(os.path.abspath(candidate)).relative_to(source)
            except (TypeError, ValueError):
                return False
            return True

        def guarded_lstat(path, *args, **kwargs):
            if is_external(path):
                raise AssertionError(f"external lstat: {path}")
            return original_lstat(path, *args, **kwargs)

        def guarded_read_bytes(path, *args, **kwargs):
            if is_external(path):
                raise AssertionError(f"external read: {path}")
            return original_read_bytes(path, *args, **kwargs)

        def guarded_open(path, *args, **kwargs):
            if is_external(path):
                raise AssertionError(f"external open: {path}")
            return original_open(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "lstat", new=guarded_lstat),
            mock.patch.object(Path, "read_bytes", new=guarded_read_bytes),
            mock.patch.object(os, "open", new=guarded_open),
        ):
            yield

    def test_v2_refresh_apply_and_recover_never_read_external_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()

            refresh_temp = temp / "refresh"
            refresh_temp.mkdir()
            migration, loop_root, source, manifest_path, before = self.scan_global(
                refresh_temp
            )
            with self.deny_external_source_io(source):
                refreshed = migration.refresh_migration(loop_root, manifest_path)
            self.assertEqual(refreshed["files"], before["files"])

            project = temp / "apply-project"
            source = project / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            loop_root = temp / "apply-loop"
            scanned = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(scanned["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = self.write_classification(
                temp / "apply-classification.json",
                manifest,
                actions=[
                    {
                        "source": "project.md",
                        "destination": "project/project.md",
                        "mode": "copy",
                    }
                ],
            )
            with self.deny_external_source_io(source):
                completed = migration.apply_migration(
                    loop_root, manifest_path, classification_path
                )
            self.assertEqual(completed["state"], "complete")

            recover_temp = temp / "recover"
            recover_temp.mkdir()
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                recover_temp
            )
            with self.deny_external_source_io(source):
                recovered = migration.recover_migration(loop_root, manifest_path)
            self.assertEqual(recovered["state"], "inventoried")

    def test_v2_detected_apply_and_recover_never_use_external_source_helpers(self):
        for operation in ("apply", "recover"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp
                )
                detected = dict(manifest)
                detected["state"] = "detected"
                self.persist_manifest(migration, loop_root, manifest_path, detected)
                ledger_path = loop_root / "migrations" / "ledger.jsonl"
                ledger_path.write_text(
                    json.dumps(
                        {
                            "migration_id": detected["migration_id"],
                            "state": "detected",
                            "timestamp": 1,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                classification_path = self.write_classification(
                    temp / "classification.json", detected
                )

                with (
                    self.deny_external_source_io(source),
                    mock.patch.object(
                        migration,
                        "_reinventoried_manifest",
                        side_effect=AssertionError("v2 apply reinventoried external source"),
                    ),
                    mock.patch.object(
                        migration,
                        "_verify_detected_state",
                        side_effect=AssertionError("v2 recover probed external source"),
                    ),
                ):
                    result = (
                        migration.apply_migration(
                            loop_root, manifest_path, classification_path
                        )
                        if operation == "apply"
                        else migration.recover_migration(loop_root, manifest_path)
                    )

                self.assertEqual(
                    result["state"],
                    "complete" if operation == "apply" else "detected",
                )

    def test_v1_external_apply_and_recover_require_legacy_stage(self):
        for operation in ("apply", "recover"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp
                )
                v1 = dict(manifest)
                v1["schema_version"] = 1
                v1["target"] = str(Path(manifest["target"]))
                v1.pop("snapshot")
                v1.pop("source_inventory_sha256")
                migration.write_json_atomic(manifest_path, v1)
                classification_path = self.write_classification(
                    temp / "classification.json", v1
                )

                with self.deny_external_source_io(source):
                    error = self.assert_migration_error(
                        "legacy_stage_required",
                        True,
                        lambda: (
                            migration.apply_migration(
                                loop_root, manifest_path, classification_path
                            )
                            if operation == "apply"
                            else migration.recover_migration(loop_root, manifest_path)
                        ),
                    )
                self.assertIn("legacy-stage", error.message)

    def test_v2_custody_digest_binds_manifest_files_and_snapshot(self):
        for corruption in ("source_digest", "files_and_digest"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
                classification_path = self.write_classification(
                    temp / "classification.json", manifest
                )
                corrupt = dict(manifest)
                if corruption == "source_digest":
                    corrupt["source_inventory_sha256"] = "f" * 64
                else:
                    corrupt["files"] = [dict(record) for record in manifest["files"]]
                    corrupt["files"][0]["sha256"] = "e" * 64
                    corrupt["source_inventory_sha256"] = migration._inventory_sha256(
                        corrupt["files"]
                    )
                self.persist_manifest(migration, loop_root, manifest_path, corrupt)

                self.assert_migration_error(
                    "corrupt_state",
                    False,
                    lambda: migration.apply_migration(
                        loop_root, manifest_path, classification_path
                    ),
                )

    def test_v2_custody_snapshot_requires_canonical_legacy_snapshot_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            original_snapshot = loop_root / manifest["snapshot"]
            fake_snapshot = loop_root / "legacy-snapshots" / "not-a-snapshot" / "payload"
            fake_snapshot.parent.mkdir(parents=True)
            import shutil

            shutil.copytree(original_snapshot, fake_snapshot)
            corrupt = dict(manifest)
            corrupt["snapshot"] = str(fake_snapshot.relative_to(loop_root))
            self.persist_manifest(migration, loop_root, manifest_path, corrupt)
            classification_path = self.write_classification(
                temp / "classification.json", corrupt
            )

            self.assert_migration_error(
                "corrupt_state",
                False,
                lambda: migration.apply_migration(
                    loop_root, manifest_path, classification_path
                ),
            )

    def test_apply_rebinds_missing_v2_snapshot_to_unique_verified_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json", manifest
            )

            broken = dict(manifest)
            broken["snapshot"] = (
                f"migrations/quarantine/{manifest['migration_id']}/source"
            )
            migration.write_json_atomic(
                manifest_path,
                migration._manifest_storage_value(broken, loop_root),
            )

            completed = migration.apply_migration(
                loop_root, manifest_path, classification_path
            )

            self.assertEqual(completed["state"], "complete")
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["snapshot"],
                Path(manifest["snapshot"]).relative_to(loop_root).as_posix(),
            )

    def test_apply_rebinds_protected_v2_snapshot_without_relaxing_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- API_KEY=dummy-test-value\n",
            )
            self.assertTrue(manifest["protected"])
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                approved_protected=True,
            )
            broken = dict(manifest)
            broken["snapshot"] = (
                f"migrations/quarantine/{manifest['migration_id']}/source"
            )
            self.persist_manifest(migration, loop_root, manifest_path, broken)

            completed = migration.apply_migration(
                loop_root, manifest_path, classification_path
            )

            self.assertEqual(completed["state"], "complete")

    def test_apply_refuses_ambiguous_verified_snapshot_rebinding(self):
        import shutil

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            original = Path(manifest["snapshot"]).parent
            duplicate = loop_root / "legacy-snapshots" / ("l-" + "f" * 32)
            shutil.copytree(original, duplicate)
            receipt_path = duplicate / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["snapshot_id"] = duplicate.name
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            classification_path = self.write_classification(
                temp / "classification.json", manifest
            )
            broken = dict(manifest)
            broken["snapshot"] = (
                f"migrations/quarantine/{manifest['migration_id']}/source"
            )
            self.persist_manifest(migration, loop_root, manifest_path, broken)

            self.assert_migration_error(
                "migration_conflict",
                True,
                lambda: migration.apply_migration(
                    loop_root, manifest_path, classification_path
                ),
            )

    def test_converted_v1_quarantine_is_accepted_as_internal_custody(self):
        import shutil

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, _, _, manifest = self.scan_global(temp)
            payload = loop_root / manifest["snapshot"]
            quarantine = (
                loop_root
                / "migrations"
                / "quarantine"
                / manifest["migration_id"]
                / "source"
            )
            quarantine.parent.mkdir(parents=True)
            shutil.copytree(payload, quarantine)
            converted = dict(manifest)
            converted["snapshot"] = quarantine.relative_to(loop_root).as_posix()

            custody = migration._manifest_custody_snapshot(loop_root, converted)

            self.assertEqual(custody.path, quarantine)
            self.assertEqual(custody.files, manifest["files"])

    def test_converted_v1_quarantine_rejects_wrong_binding_and_content(self):
        import shutil

        for case in ("wrong-id", "wrong-path", "symlink", "digest"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                migration, loop_root, _, _, manifest = self.scan_global(temp)
                payload = loop_root / manifest["snapshot"]
                quarantine = (
                    loop_root
                    / "migrations"
                    / "quarantine"
                    / manifest["migration_id"]
                    / "source"
                )
                quarantine.parent.mkdir(parents=True)
                shutil.copytree(payload, quarantine)
                converted = dict(manifest)
                converted["snapshot"] = quarantine.relative_to(loop_root).as_posix()
                if case == "wrong-id":
                    wrong = loop_root / "migrations/quarantine/m-ffffffffffffffffffffffffffffffff/source"
                    wrong.parent.mkdir(parents=True)
                    quarantine.rename(wrong)
                    converted["snapshot"] = wrong.relative_to(loop_root).as_posix()
                elif case == "wrong-path":
                    wrong = loop_root / "migrations/staging" / manifest["migration_id"] / "source"
                    wrong.parent.mkdir(parents=True)
                    quarantine.rename(wrong)
                    converted["snapshot"] = wrong.relative_to(loop_root).as_posix()
                elif case == "symlink":
                    displaced = quarantine.with_name("displaced")
                    quarantine.rename(displaced)
                    quarantine.symlink_to(displaced)
                else:
                    (quarantine / "long.md").write_text("changed\n", encoding="utf-8")

                self.assert_migration_error(
                    "corrupt_state",
                    False,
                    lambda: migration._manifest_custody_snapshot(loop_root, converted),
                )

    def test_v2_custody_snapshot_binds_receipt_metadata(self):
        for field, value in (
            ("source_path", "/tmp/other-legacy-memory"),
            ("inventory_sha256", "f" * 64),
            ("importable", False),
            ("protection_reasons", ["credential_assignment"]),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
                receipt_path = loop_root / manifest["snapshot"]
                receipt_path = receipt_path.parent / "receipt.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt[field] = value
                receipt_path.write_text(
                    json.dumps(receipt, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                classification_path = self.write_classification(
                    temp / "classification.json", manifest
                )

                self.assert_migration_error(
                    "corrupt_state",
                    False,
                    lambda: migration.apply_migration(
                        loop_root, manifest_path, classification_path
                    ),
                )

    def test_v2_source_read_rejects_same_content_payload_inode_swap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            source_path = loop_root / manifest["snapshot"] / "long.md"
            original_open = os.open
            swapped = False

            def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "long.md" and dir_fd is not None and not swapped:
                    swapped = True
                    displaced = source_path.with_name("displaced-long.md")
                    source_path.rename(displaced)
                    source_path.write_bytes(displaced.read_bytes())
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(os, "open", new=swap_before_open):
                self.assert_migration_error(
                    "corrupt_state",
                    False,
                    lambda: migration._read_inventoried_source_bytes(
                        loop_root, manifest, "long.md"
                    ),
                )

    def test_v2_custody_rejects_wrong_owner_without_reading_body(self):
        for target in ("snapshot_dir", "payload", "receipt", "child"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                migration, loop_root, _, _, manifest = self.scan_global(temp)
                snapshot = loop_root / manifest["snapshot"]
                selected = {
                    "snapshot_dir": snapshot.parent,
                    "payload": snapshot,
                    "receipt": snapshot.parent / "receipt.json",
                    "child": snapshot / "long.md",
                }[target]
                real_lstat = Path.lstat
                real_stat = os.stat

                def wrong_owner(path, *args, **kwargs):
                    value = real_lstat(path, *args, **kwargs)
                    if Path(path) == selected:
                        values = list(value)
                        values[4] = os.getuid() + 1
                        return os.stat_result(values)
                    return value

                def wrong_owner_child(path, *args, **kwargs):
                    value = real_stat(path, *args, **kwargs)
                    if target == "child" and path == "long.md" and kwargs.get("dir_fd") is not None:
                        values = list(value)
                        values[4] = os.getuid() + 1
                        return os.stat_result(values)
                    return value

                with (
                    mock.patch.object(Path, "lstat", new=wrong_owner),
                    mock.patch.object(os, "stat", new=wrong_owner_child),
                ):
                    self.assert_migration_error(
                        "corrupt_state",
                        False,
                        lambda: migration._manifest_custody_snapshot(
                            loop_root,
                            manifest,
                        ),
                    )

    def test_v1_quarantined_recovery_reads_only_internal_custody(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json", manifest
            )
            completed = migration.apply_migration(
                loop_root, manifest_path, classification_path
            )
            quarantine = (
                loop_root
                / "migrations"
                / "quarantine"
                / completed["migration_id"]
                / "source"
            )
            import shutil

            shutil.copytree(Path(completed["snapshot"]), quarantine)
            source.rename(temp / "retired-source")
            v1 = {
                key: value
                for key, value in completed.items()
                if key not in {"snapshot", "source_inventory_sha256"}
            }
            v1.update(
                {
                    "schema_version": 1,
                    "state": "quarantined",
                    "target": str(Path(completed["target"])),
                    "staging_path": str(Path(completed["staging_path"])),
                    "quarantine_path": str(quarantine),
                }
            )
            migration.write_json_atomic(manifest_path, v1)
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            ledger_path.write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in events
                    if event["state"] != "complete"
                ),
                encoding="utf-8",
            )

            with self.deny_external_source_io(source):
                recovered = migration.recover_migration(loop_root, manifest_path)

            self.assertEqual(recovered["state"], "complete")
            self.assertEqual(recovered["recovery"], "completed_quarantine")

    def test_scan_v2_manifest_binds_external_source_to_internal_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            project = temp / "project"
            source = project / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text("# Project Memory\n", encoding="utf-8")
            staged = legacy.stage_legacy(loop_root, project)

            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            persisted = json.loads(manifest_path.read_text())

            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(persisted["source"], str(source))
            self.assertEqual(
                persisted["snapshot"],
                Path(staged["snapshot_path"]).relative_to(loop_root).as_posix(),
            )
            self.assertEqual(
                persisted["source_inventory_sha256"],
                staged["inventory_sha256"],
            )
            self.assertFalse(Path(persisted["target"]).is_absolute())
            self.assertTrue(source.is_dir())

    def test_scan_ignores_noncanonical_matching_snapshot_and_stages_canonical(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            project = temp / "project"
            source = project / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text("# Project\n", encoding="utf-8")
            files, _ = migration._inventory_files(source)
            digest = migration._inventory_sha256(files)
            fake = loop_root / "legacy-snapshots" / "not-canonical"
            fake.mkdir(parents=True)
            (fake / "payload").mkdir()
            (fake / "payload" / "long.md").write_text("# Project\n", encoding="utf-8")
            (fake / "receipt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "snapshot_id": "not-canonical",
                        "source_path": str(source),
                        "inventory_sha256": digest,
                        "importable": True,
                        "protection_reasons": [],
                    }
                ),
                encoding="utf-8",
            )

            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertRegex(
                persisted["snapshot"],
                r"^legacy-snapshots/l-[0-9a-f]{32}/payload$",
            )
            self.assertNotIn("not-canonical", manifest["snapshot"])

    def test_scan_does_not_publish_receipt_drift_or_wrong_owner_candidate(self):
        for corruption in ("receipt", "owner"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                loop_root = temp / "loop"
                project = temp / "project"
                source = project / ".memory"
                source.mkdir(parents=True)
                (source / "long.md").write_text("# Project\n", encoding="utf-8")
                files, _ = migration._inventory_files(source)
                digest = migration._inventory_sha256(files)
                snapshot_id = "l-" + "a" * 32
                snapshot_dir = loop_root / "legacy-snapshots" / snapshot_id
                payload = snapshot_dir / "payload"
                payload.mkdir(parents=True)
                (payload / "long.md").write_text("# Project\n", encoding="utf-8")
                receipt = {
                    "schema_version": 2,
                    "snapshot_id": snapshot_id,
                    "source_path": str(source),
                    "inventory_sha256": digest,
                    "importable": True,
                    "protection_reasons": [],
                }
                if corruption == "receipt":
                    receipt["source_path"] = str(temp / "other" / ".memory")
                (snapshot_dir / "receipt.json").write_text(
                    json.dumps(receipt),
                    encoding="utf-8",
                )
                real_lstat = Path.lstat

                def wrong_owner(path, *args, **kwargs):
                    value = real_lstat(path, *args, **kwargs)
                    if corruption == "owner" and Path(path) == payload:
                        values = list(value)
                        values[4] = os.getuid() + 1
                        return os.stat_result(values)
                    return value

                with mock.patch.object(Path, "lstat", new=wrong_owner):
                    result = migration.scan_legacy(loop_root, project, [])

                self.assertEqual(len(result["manifests"]), 1)
                manifest_path = Path(result["manifests"][0])
                persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertRegex(
                    persisted["snapshot"],
                    r"^legacy-snapshots/l-[0-9a-f]{32}/payload$",
                )
                self.assertNotEqual(
                    persisted["snapshot"],
                    f"legacy-snapshots/{snapshot_id}/payload",
                )
                ledger = loop_root / "migrations" / "ledger.jsonl"
                self.assertNotIn(snapshot_id, ledger.read_text(encoding="utf-8"))

    def test_staging_reads_snapshot_after_external_source_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            project = temp / "project"
            source = project / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            (source / "project.md").write_text("external drift\n", encoding="utf-8")

            content = migration._read_inventoried_source_bytes(
                loop_root, manifest, "project.md"
            )

            self.assertEqual(content, PROJECT_TEMPLATE.encode())

    def migration_module(self):
        try:
            return importlib.import_module("scripts.loopmem.migration")
        except ModuleNotFoundError:
            self.fail("scripts.loopmem.migration has not been implemented")

    def assert_migration_error(self, code, recoverable, operation):
        with self.assertRaises(LoopMemoryError) as context:
            operation()
        self.assertEqual(context.exception.code, code)
        self.assertEqual(context.exception.recoverable, recoverable)
        return context.exception

    def assert_source_unstable(self, operation):
        error = self.assert_migration_error("source_unstable", True, operation)
        self.assertEqual(
            error.message,
            "Legacy source changed or could not be read consistently",
        )
        return error

    def assert_unsafe_legacy_source(self, operation):
        return self.assert_migration_error(
            "unsafe_legacy_source",
            False,
            operation,
        )

    def mock_canonical_global(self, migration, source: Path) -> None:
        patcher = mock.patch.object(
            migration,
            "_canonical_legacy_global_root",
            return_value=source.resolve(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan_global(self, temp: Path, *, body: str = "- legacy\n"):
        cwd = temp / "home"
        source = cwd / ".memory"
        source.mkdir(parents=True)
        (source / "long.md").write_text(
            f"# Legacy Long\n\n## Entries\n\n{body}",
            encoding="utf-8",
        )
        loop_root = temp / "loop"
        migration = self.migration_module()
        self.mock_canonical_global(migration, source)
        result = migration.scan_legacy(loop_root, cwd, [])
        manifest_path = Path(result["manifests"][0])
        return migration, loop_root, source, manifest_path, migration.load_manifest(
            manifest_path
        )

    def write_classification(
        self,
        path: Path,
        manifest: dict[str, object],
        **changes,
    ) -> Path:
        value = {
            "migration_id": manifest["migration_id"],
            "actions": [
                {
                    "source": "long.md",
                    "destination": "global/long.md",
                    "mode": "merge_entries",
                }
            ],
            "reference_updates": [],
            "approved_protected": False,
        }
        value.update(changes)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def persist_manifest(self, migration, loop_root: Path, path: Path, value):
        """Persist one runtime/inflated manifest as valid schema-v2 storage."""
        stored = migration._manifest_storage_value(dict(value), loop_root)
        migration.write_json_atomic(path, stored)
        return stored

    def interrupt_after_manifest_state(
        self,
        migration,
        loop_root: Path,
        manifest_path: Path,
        classification_path: Path,
        state: str,
    ) -> dict[str, object]:
        original_ledger_event = migration._ensure_ledger_event
        interrupted = False

        def interrupt(root, migration_id, current_state):
            nonlocal interrupted
            if current_state == state and not interrupted:
                interrupted = True
                raise RuntimeError(f"interrupt after {state} manifest")
            return original_ledger_event(root, migration_id, current_state)

        with mock.patch.object(
            migration,
            "_ensure_ledger_event",
            side_effect=interrupt,
        ):
            with self.assertRaisesRegex(RuntimeError, f"after {state} manifest"):
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )
        return migration.load_manifest(manifest_path)

    def test_scan_global_memory_writes_deterministic_reusable_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            loop_root = temp / "loop"
            source.mkdir(parents=True)
            bodies = {
                "long.md": b"# Long\n",
                "medium.md": b"# Medium\n",
                "short.md": b"# Short\n",
            }
            for name, body in bodies.items():
                (source / name).write_bytes(body)

            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            first = migration.scan_legacy(loop_root, cwd, [source])
            second = migration.scan_legacy(loop_root, cwd, [source])

            self.assertEqual(first["excluded"], [])
            self.assertEqual(first["warnings"], [])
            self.assertEqual(second, first)
            self.assertEqual(len(first["manifests"]), 1)
            manifest_path = Path(first["manifests"][0])
            before_load = manifest_path.read_bytes()
            manifest = migration.load_manifest(manifest_path)
            self.assertEqual(manifest_path.read_bytes(), before_load)
            self.assertEqual(
                set(manifest),
                {
                    "migration_id",
                    "schema_version",
                    "state",
                    "source",
                    "source_kind",
                    "project_id",
                    "catalogued_files",
                    "files",
                    "snapshot",
                    "source_inventory_sha256",
                    "target",
                    "created_at",
                    "updated_at",
                    "warnings",
                },
            )
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["state"], "inventoried")
            self.assertEqual(manifest["source"], str(source.resolve()))
            self.assertEqual(manifest["source_kind"], "global")
            self.assertIsNone(manifest["project_id"])
            self.assertEqual(manifest["catalogued_files"], [])
            self.assertEqual(manifest["target"], str((loop_root / "global").resolve()))
            self.assertEqual(
                manifest["files"],
                [
                    {
                        "relative_path": name,
                        "sha256": hashlib.sha256(bodies[name]).hexdigest(),
                        "size": len(bodies[name]),
                    }
                    for name in sorted(bodies)
                ],
            )
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["target"], "global")
            self.assertFalse(Path(persisted["snapshot"]).is_absolute())
            self.assertEqual(
                persisted["source_inventory_sha256"],
                migration._inventory_sha256(manifest["files"]),
            )
            self.assertEqual(
                manifest["snapshot"],
                str((loop_root / persisted["snapshot"]).resolve()),
            )
            self.assertTrue(source.is_dir())

    def test_scan_empty_project_maps_registry_identity_without_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            loop_root = temp / "loop"

            result = self.migration_module().scan_legacy(loop_root, cwd, [])
            manifest = self.migration_module().load_manifest(
                Path(result["manifests"][0])
            )

            self.assertEqual(manifest["source_kind"], "empty")
            self.assertRegex(manifest["project_id"], r"^p-[0-9a-f]{32}$")
            self.assertEqual(manifest["files"], [])
            self.assertEqual(
                manifest["target"],
                str((loop_root / "projects" / manifest["project_id"]).resolve()),
            )
            self.assertFalse((loop_root / "projects").exists())

    def test_scan_recursively_identifies_legacy_session_and_stable_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            status = source / "agents" / "codex" / "main" / "status.md"
            status.parent.mkdir(parents=True)
            status.write_text("# Legacy status\n", encoding="utf-8")
            (source / "z.txt").write_text("last", encoding="utf-8")
            loop_root = temp / "loop"

            result = self.migration_module().scan_legacy(loop_root, cwd, [])
            manifest = self.migration_module().load_manifest(
                Path(result["manifests"][0])
            )

            self.assertEqual(manifest["source_kind"], "session")
            self.assertRegex(manifest["project_id"], r"^p-[0-9a-f]{32}$")
            self.assertEqual(
                [record["relative_path"] for record in manifest["files"]],
                ["agents/codex/main/status.md", "z.txt"],
            )
            target = Path(manifest["target"])
            self.assertEqual(target.name, f"s-legacy-{manifest['migration_id'][2:]}")
            self.assertRegex(target.parent.name, r"^\d{4}-(?:0[1-9]|1[0-2])$")
            self.assertEqual(target.parent.parent.name, "archive")

    def test_scan_uses_actual_cwd_and_keeps_explicit_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            cwd = project / "nested" / "working"
            cwd.mkdir(parents=True)
            cwd_memory = cwd / ".memory"
            cwd_memory.mkdir()
            (cwd_memory / "project.md").write_text(
                "# Current directory legacy\n",
                encoding="utf-8",
            )
            explicit_memory = temp / "explicit" / ".memory"
            explicit_memory.mkdir(parents=True)
            (explicit_memory / "project.md").write_text(
                "# Explicit legacy\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()

            result = migration.scan_legacy(
                loop_root,
                cwd,
                [explicit_memory],
            )

            manifests = [
                migration.load_manifest(Path(path)) for path in result["manifests"]
            ]
            self.assertEqual(
                {manifest["source"] for manifest in manifests},
                {
                    str(cwd_memory.resolve()),
                    str(explicit_memory.resolve()),
                },
            )
            self.assertNotIn(str(project / ".memory"), "\n".join(result["warnings"]))

    def test_scan_serializes_manifest_deduplication_and_writes_inventoried_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            barrier = threading.Barrier(2)
            original_inventory = migration._inventory_files

            def synchronized_inventory(path):
                try:
                    barrier.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
                return original_inventory(path)

            results = []
            errors = []

            def scan_once():
                try:
                    results.append(migration.scan_legacy(loop_root, cwd, []))
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(
                migration,
                "_inventory_files",
                side_effect=synchronized_inventory,
            ):
                threads = [threading.Thread(target=scan_once) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(
                len(list((loop_root / "migrations" / "manifests").glob("*.json"))),
                1,
            )
            self.assertEqual(
                sum(len(result["manifests"]) for result in results),
                1,
            )
            self.assertTrue(
                not errors
                or all(
                    isinstance(error, LoopMemoryError) and error.code == "lease_busy"
                    for error in errors
                )
            )
            manifest_path = next(
                (loop_root / "migrations" / "manifests").glob("*.json")
            )
            manifest = migration.load_manifest(manifest_path)
            ledger = [
                json.loads(line)
                for line in (loop_root / "migrations" / "ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                ledger,
                [
                    {
                        "migration_id": manifest["migration_id"],
                        "state": "inventoried",
                        "timestamp": ledger[0]["timestamp"],
                    }
                ],
            )

    def test_scan_blocks_symlink_root_and_descendant_without_inventory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            cwd.mkdir()
            external = temp / "external"
            external.mkdir()
            (external / "secret.txt").write_text("must not inventory", encoding="utf-8")
            linked_root = cwd / ".memory"
            linked_root.symlink_to(external, target_is_directory=True)
            descendant_root = temp / "other" / ".memory"
            descendant_root.mkdir(parents=True)
            (descendant_root / "safe.txt").write_text("safe", encoding="utf-8")
            (descendant_root / "linked.txt").symlink_to(external / "secret.txt")
            loop_root = temp / "loop"

            result = self.migration_module().scan_legacy(
                loop_root,
                cwd,
                [descendant_root],
            )

            self.assertEqual(result["manifests"], [])
            self.assertEqual(
                result["excluded"],
                [
                    {
                        "path": str(linked_root.parent.resolve() / linked_root.name),
                        "reason": "unsafe_legacy_source",
                    },
                    {
                        "path": str(descendant_root.absolute()),
                        "reason": "unsafe_legacy_source",
                    },
                ],
            )

    def test_scan_contains_symlink_scandir_disappearance_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            migration = self.migration_module()
            original_scandir = os.scandir
            injected = False

            def disappearing_scandir(path):
                nonlocal injected
                if isinstance(path, int) and not injected:
                    injected = True
                    raise FileNotFoundError("raw scandir disappearance")
                return original_scandir(path)

            with mock.patch.object(
                migration.os,
                "scandir",
                side_effect=disappearing_scandir,
            ):
                self.assert_source_unstable(
                    lambda: migration.scan_legacy(temp / "loop", cwd, [])
                )

            self.assertTrue(injected)

    def test_scan_contains_symlink_entry_stat_disappearance_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            path = source / "project.md"
            path.write_text("# Legacy\n", encoding="utf-8")
            migration = self.migration_module()
            original_stat = os.stat
            injected = False

            def disappearing_entry_stat(
                entry_path,
                *args,
                dir_fd=None,
                follow_symlinks=True,
                **kwargs,
            ):
                nonlocal injected
                if (
                    dir_fd is not None
                    and Path(entry_path) == Path("project.md")
                    and not injected
                ):
                    injected = True
                    raise FileNotFoundError("raw entry disappearance")
                return original_stat(
                    entry_path,
                    *args,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                    **kwargs,
                )

            with mock.patch.object(
                migration.os,
                "stat",
                side_effect=disappearing_entry_stat,
            ):
                self.assert_source_unstable(
                    lambda: migration.scan_legacy(temp / "loop", cwd, [])
                )

            self.assertTrue(injected)

    def test_scan_root_lstat_errors_are_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text("# Legacy\n", encoding="utf-8")
            migration = self.migration_module()
            original_lstat = Path.lstat
            resolved_source = source.resolve()

            for index, root_error in enumerate(
                (
                    PermissionError("raw root permission detail"),
                    OSError(errno.EIO, "raw root io detail"),
                )
            ):
                def failing_root_lstat(path, *args, **kwargs):
                    if Path(path) == resolved_source:
                        raise root_error
                    return original_lstat(path, *args, **kwargs)

                with self.subTest(error=type(root_error).__name__), mock.patch.object(
                    type(source),
                    "lstat",
                    autospec=True,
                    side_effect=failing_root_lstat,
                ):
                    self.assert_source_unstable(
                        lambda: migration.scan_legacy(
                            temp / f"loop-{index}",
                            cwd,
                            [],
                        )
                    )

    def test_scan_rejects_raced_ancestor_symlink_without_hashing_outside_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            child = source / "child"
            child.mkdir(parents=True)
            (child / "inside.md").write_text("inside\n", encoding="utf-8")
            outside = temp / "outside"
            outside.mkdir()
            sentinel = b"outside sentinel must never be inventoried\n"
            (outside / "sentinel.md").write_bytes(sentinel)
            sentinel_digest = hashlib.sha256(sentinel).hexdigest()
            saved_child = temp / "saved-child"
            migration = self.migration_module()
            original_contains_symlink = migration._contains_symlink
            original_open = os.open
            original_scandir = os.scandir
            precheck_complete = False
            replaced = False

            def checked_contains_symlink(path):
                nonlocal precheck_complete
                result = original_contains_symlink(path)
                if Path(path).name == ".memory":
                    precheck_complete = True
                return result

            def replace_child():
                nonlocal replaced
                if precheck_complete and not replaced:
                    child.rename(saved_child)
                    child.symlink_to(outside, target_is_directory=True)
                    replaced = True

            def raced_scandir(directory):
                if not isinstance(directory, int) and Path(directory).name == "child":
                    replace_child()
                return original_scandir(directory)

            def raced_open(path, flags, mode=0o777, *, dir_fd=None):
                if (
                    dir_fd is not None
                    and Path(path) == Path("child")
                    and flags & os.O_DIRECTORY
                ):
                    replace_child()
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        migration,
                        "_contains_symlink",
                        side_effect=checked_contains_symlink,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        migration.os,
                        "scandir",
                        side_effect=raced_scandir,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        migration.os,
                        "open",
                        side_effect=raced_open,
                    )
                )
                try:
                    result = migration.scan_legacy(temp / "loop", cwd, [])
                except LoopMemoryError as error:
                    self.assertEqual(error.code, "unsafe_legacy_source")
                    self.assertFalse(error.recoverable)
                else:
                    self.assertTrue(replaced)
                    manifest = migration.load_manifest(Path(result["manifests"][0]))
                    digests = {record["sha256"] for record in manifest["files"]}
                    self.assertNotIn(sentinel_digest, digests)
                    self.fail("scan accepted a raced ancestor symlink")

            self.assertTrue(replaced)

    def test_scan_rejects_child_symlink_inserted_after_directory_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            child = source / "child"
            child.mkdir(parents=True)
            (child / "inside.md").write_text("inside\n", encoding="utf-8")
            outside = temp / "outside"
            outside.mkdir()
            saved_child = temp / "saved-child"
            loop_root = temp / "loop"
            migration = self.migration_module()
            original_contains_symlink = migration._contains_symlink
            original_open = os.open
            precheck_complete = False
            replaced = False

            def checked_contains_symlink(path):
                nonlocal precheck_complete
                result = original_contains_symlink(path)
                if Path(path).name == ".memory":
                    precheck_complete = True
                return result

            def replace_after_child_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal replaced
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    precheck_complete
                    and not replaced
                    and dir_fd is not None
                    and Path(path) == Path("child")
                    and flags & os.O_DIRECTORY
                ):
                    child.rename(saved_child)
                    child.symlink_to(outside, target_is_directory=True)
                    replaced = True
                return descriptor

            with mock.patch.object(
                migration,
                "_contains_symlink",
                side_effect=checked_contains_symlink,
            ), mock.patch.object(
                migration.os,
                "open",
                side_effect=replace_after_child_open,
            ):
                try:
                    migration.scan_legacy(loop_root, cwd, [])
                except LoopMemoryError as error:
                    self.assertEqual(error.code, "unsafe_legacy_source")
                    self.assertFalse(error.recoverable)
                else:
                    self.fail("scan published after a child became a symlink")

            self.assertTrue(replaced)
            manifests = loop_root / "migrations" / "manifests"
            self.assertEqual(list(manifests.glob("*.json")), [])

    def test_inventory_scandir_disappearance_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            migration = self.migration_module()

            with mock.patch.object(
                migration.os,
                "scandir",
                side_effect=FileNotFoundError,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source)
                )

            entries = mock.MagicMock()
            entries.__iter__.side_effect = PermissionError("raw iteration detail")
            with mock.patch.object(
                migration.os,
                "scandir",
                return_value=entries,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source),
                )

    def test_inventory_rejects_root_symlink_inserted_after_directory_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / ".memory"
            source.mkdir()
            (source / "inside.md").write_text("inside\n", encoding="utf-8")
            outside = temp / "outside"
            outside.mkdir()
            saved_source = temp / "saved-memory"
            migration = self.migration_module()
            original_open = os.open
            replaced = False

            def replace_after_root_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal replaced
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    not replaced
                    and dir_fd is None
                    and Path(path) == source
                    and flags & os.O_DIRECTORY
                ):
                    source.rename(saved_source)
                    source.symlink_to(outside, target_is_directory=True)
                    replaced = True
                return descriptor

            with mock.patch.object(
                migration.os,
                "open",
                side_effect=replace_after_root_open,
            ):
                self.assert_unsafe_legacy_source(
                    lambda: migration._inventory_files(source)
                )

            self.assertTrue(replaced)

    def test_inventory_root_regular_replacement_before_open_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / ".memory"
            source.mkdir()
            (source / "inside.md").write_text("inside\n", encoding="utf-8")
            saved_source = temp / "saved-memory"
            migration = self.migration_module()
            original_open = os.open
            replaced = False

            def replace_before_root_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal replaced
                if (
                    not replaced
                    and dir_fd is None
                    and Path(path) == source
                    and flags & os.O_DIRECTORY
                ):
                    source.rename(saved_source)
                    source.write_text("replacement\n", encoding="utf-8")
                    replaced = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(
                migration.os,
                "open",
                side_effect=replace_before_root_open,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source)
                )

            self.assertTrue(replaced)

    def test_inventory_child_regular_replacement_before_open_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / ".memory"
            child = source / "child"
            child.mkdir(parents=True)
            (child / "inside.md").write_text("inside\n", encoding="utf-8")
            saved_child = temp / "saved-child"
            migration = self.migration_module()
            original_open = os.open
            replaced = False

            def replace_before_child_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal replaced
                if (
                    not replaced
                    and dir_fd is not None
                    and Path(path) == Path("child")
                    and flags & os.O_DIRECTORY
                ):
                    child.rename(saved_child)
                    child.write_text("replacement\n", encoding="utf-8")
                    replaced = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(
                migration.os,
                "open",
                side_effect=replace_before_child_open,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source)
                )

            self.assertTrue(replaced)

    def test_inventory_entry_stat_disappearance_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            (source / "legacy.md").write_text("# Legacy\n", encoding="utf-8")
            migration = self.migration_module()
            original_stat = os.stat
            injected = False

            def disappearing_entry_stat(
                path,
                *args,
                dir_fd=None,
                follow_symlinks=True,
                **kwargs,
            ):
                nonlocal injected
                if dir_fd is not None and Path(path) == Path("legacy.md"):
                    injected = True
                    raise FileNotFoundError("raw entry disappearance")
                return original_stat(
                    path,
                    *args,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                    **kwargs,
                )

            with mock.patch.object(
                migration.os,
                "stat",
                side_effect=disappearing_entry_stat,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source),
                )

            self.assertTrue(injected)

    def test_inventory_read_disappearance_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            (source / "legacy.md").write_text("# Legacy\n", encoding="utf-8")
            migration = self.migration_module()
            original_open = os.open

            for boundary in ("open", "read"):
                injected = False

                def disappearing_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal injected
                    if dir_fd is not None and Path(path) == Path("legacy.md"):
                        injected = True
                        raise FileNotFoundError("raw open detail")
                    return original_open(path, flags, mode, dir_fd=dir_fd)

                def disappearing_read(descriptor, size):
                    nonlocal injected
                    injected = True
                    raise FileNotFoundError("raw read detail")

                operation = disappearing_open if boundary == "open" else disappearing_read
                with self.subTest(boundary=boundary), mock.patch.object(
                    migration.os,
                    boundary,
                    side_effect=operation,
                ) as patched_operation:
                    self.assert_source_unstable(
                        lambda: migration._inventory_files(source),
                    )

                    self.assertTrue(injected)
                    patched_operation.assert_called()
                    if boundary == "open":
                        file_call = next(
                            call
                            for call in patched_operation.call_args_list
                            if call.kwargs.get("dir_fd") is not None
                        )
                        flags = file_call.args[1]
                        self.assertTrue(flags & migration.os.O_NOFOLLOW)

    def test_inventory_post_read_identity_mismatch_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            path = source / "legacy.md"
            path.write_text("# Legacy\n", encoding="utf-8")
            initial_stat = path.stat(follow_symlinks=False)
            mismatched_stat = mock.Mock(
                st_mode=initial_stat.st_mode,
                st_dev=initial_stat.st_dev,
                st_ino=initial_stat.st_ino + 1,
                st_size=initial_stat.st_size,
                st_mtime_ns=initial_stat.st_mtime_ns,
                st_ctime_ns=initial_stat.st_ctime_ns,
            )
            migration = self.migration_module()
            original_fstat = os.fstat
            original_stat = os.stat
            relative_stat_calls = 0

            def mismatched_post_read_stat(
                stat_path,
                *args,
                dir_fd=None,
                follow_symlinks=True,
                **kwargs,
            ):
                nonlocal relative_stat_calls
                result = original_stat(
                    stat_path,
                    *args,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                    **kwargs,
                )
                if dir_fd is not None and Path(stat_path) == Path("legacy.md"):
                    relative_stat_calls += 1
                    if relative_stat_calls == 2:
                        return mismatched_stat
                return result

            with mock.patch.object(
                migration.os,
                "fstat",
                wraps=original_fstat,
            ) as descriptor_stat, mock.patch.object(
                migration.os,
                "stat",
                side_effect=mismatched_post_read_stat,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source),
                )

            self.assertGreaterEqual(descriptor_stat.call_count, 3)
            self.assertEqual(relative_stat_calls, 2)

    def test_inventory_in_place_rewrite_during_read_is_source_unstable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            path = source / "legacy.md"
            chunk_size = 1024 * 1024
            path.write_bytes((b"a" * chunk_size) + (b"b" * chunk_size))
            replacement = b"c" * (2 * chunk_size)
            torn_digest = hashlib.sha256(
                (b"a" * chunk_size) + (b"c" * chunk_size)
            ).hexdigest()
            migration = self.migration_module()
            original_read = os.read
            rewritten = False

            def rewrite_after_first_chunk(descriptor, size):
                nonlocal rewritten
                content = original_read(descriptor, size)
                if content and not rewritten:
                    path.write_bytes(replacement)
                    rewritten = True
                return content

            with mock.patch.object(
                migration.os,
                "read",
                side_effect=rewrite_after_first_chunk,
            ):
                try:
                    files, _ = migration._inventory_files(source)
                except LoopMemoryError as error:
                    self.assertEqual(error.code, "source_unstable")
                    self.assertTrue(error.recoverable)
                else:
                    self.assertNotEqual(files[0]["sha256"], torn_digest)
                    self.fail("inventory accepted content rewritten during read")

            self.assertTrue(rewritten)

    def test_inventory_rejects_symlink_and_special_file_as_unsafe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration = self.migration_module()

            def assert_unsafe_source(source):
                return self.assert_unsafe_legacy_source(
                    lambda: migration._inventory_files(source),
                )

            symlink_source = temp / "symlink" / ".memory"
            symlink_source.mkdir(parents=True)
            (symlink_source / "linked.md").symlink_to(temp / "outside.md")
            with self.subTest(kind="symlink"):
                assert_unsafe_source(symlink_source)

            special_source = temp / "special" / ".memory"
            special_source.mkdir(parents=True)
            os.mkfifo(special_source / "legacy.pipe")
            with self.subTest(kind="special"):
                assert_unsafe_source(special_source)

            unsafe_path_source = temp / "unsafe-path" / ".memory"
            unsafe_path_source.mkdir(parents=True)
            unsafe_entry = mock.Mock(
                path=str(temp / "outside.md"),
            )
            unsafe_entry.name = "../outside.md"
            unsafe_entry.stat.return_value = mock.Mock(
                st_mode=stat.S_IFREG | 0o600,
            )
            unsafe_entries = mock.MagicMock()
            unsafe_entries.__iter__.return_value = iter([unsafe_entry])
            with self.subTest(kind="unsafe-relative-path"), mock.patch.object(
                migration.os,
                "scandir",
                return_value=unsafe_entries,
            ):
                assert_unsafe_source(unsafe_path_source)

            refusal_source = temp / "refusal" / ".memory"
            refusal_source.mkdir(parents=True)
            (refusal_source / "legacy.md").write_text(
                "# Legacy\n",
                encoding="utf-8",
            )
            refusal = OSError(errno.ELOOP, "raw refusal detail")
            original_open = os.open
            refused = False

            def refuse_file_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal refused
                if dir_fd is not None and Path(path) == Path("legacy.md"):
                    refused = True
                    raise refusal
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with self.subTest(kind="no-follow-refusal"), mock.patch.object(
                migration.os,
                "open",
                side_effect=refuse_file_open,
            ):
                error = assert_unsafe_source(refusal_source)
                self.assertNotIn("raw refusal detail", error.message)
                self.assertTrue(refused)

            original_fstat = os.fstat

            def special_opened_file(descriptor):
                descriptor_stat = original_fstat(descriptor)
                if stat.S_ISREG(descriptor_stat.st_mode):
                    return mock.Mock(st_mode=stat.S_IFIFO | 0o600)
                return descriptor_stat

            with self.subTest(kind="fstat-special"), mock.patch.object(
                migration.os,
                "fstat",
                side_effect=special_opened_file,
            ):
                assert_unsafe_source(refusal_source)

            for flag, value in (
                ("O_NOFOLLOW", None),
                ("O_NOFOLLOW", 0),
                ("O_DIRECTORY", None),
                ("O_DIRECTORY", 0),
            ):
                with self.subTest(
                    kind="required-flag-unavailable",
                    flag=flag,
                    value=value,
                ), mock.patch.object(migration.os, flag, value):
                    assert_unsafe_source(refusal_source)

    def test_inventory_close_failure_preserves_active_unsafe_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            (source / "legacy.md").write_text("# Legacy\n", encoding="utf-8")
            migration = self.migration_module()
            original_close = os.close

            def close_then_fail(descriptor):
                original_close(descriptor)
                raise OSError(errno.EIO, "raw close detail")

            with mock.patch.object(
                migration.os,
                "fstat",
                return_value=mock.Mock(st_mode=stat.S_IFIFO | 0o600),
            ), mock.patch.object(
                migration.os,
                "close",
                side_effect=close_then_fail,
            ):
                self.assert_unsafe_legacy_source(
                    lambda: migration._inventory_files(source)
                )

            with mock.patch.object(
                migration.os,
                "close",
                side_effect=close_then_fail,
            ):
                self.assert_source_unstable(
                    lambda: migration._inventory_files(source)
                )

    def test_inventory_requires_nofollow_stat_capability(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            (source / "legacy.md").write_text("# Legacy\n", encoding="utf-8")
            migration = self.migration_module()
            supported = set(os.supports_follow_symlinks)
            supported.discard(os.stat)

            with mock.patch.object(
                migration.os,
                "supports_follow_symlinks",
                supported,
            ):
                self.assert_unsafe_legacy_source(
                    lambda: migration._inventory_files(source)
                )

    def test_stable_source_snapshot_requires_identical_passes(self):
        migration = self.migration_module()
        source = Path("/legacy/.memory")
        first_file = {
            "relative_path": "project.md",
            "sha256": "a" * 64,
            "size": 1,
        }
        changed_file = {
            "relative_path": "project.md",
            "sha256": "b" * 64,
            "size": 1,
        }
        baseline = migration.SourceSnapshot(
            files=[first_file],
            catalogued_files=["project.md"],
            has_credential_assignment=False,
        )
        changed_snapshots = (
            replace(baseline, files=[changed_file]),
            replace(baseline, catalogued_files=[]),
            replace(baseline, has_credential_assignment=True),
        )
        self.assertTrue(all(baseline != changed for changed in changed_snapshots))

        with mock.patch.object(
            migration,
            "_inventory_files",
            side_effect=[([first_file], False), ([changed_file], False)],
        ) as inventory, mock.patch.object(
            migration,
            "_observation_snapshot",
            side_effect=[(["project.md"], True), (["project.md"], True)],
        ) as tracking:
            self.assert_source_unstable(
                lambda: migration._stable_source_snapshot(source, "project"),
            )

        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(
            tracking.call_args_list,
            [mock.call(source, "project"), mock.call(source, "project")],
        )

        with mock.patch.object(
            migration,
            "_inventory_files",
            side_effect=[([first_file], False), ([first_file], False)],
        ) as inventory, mock.patch.object(
            migration,
            "_observation_snapshot",
            side_effect=[(["project.md"], False), (["project.md"], True)],
        ) as tracking:
            self.assert_migration_error(
                "observation_unknown",
                False,
                lambda: migration._stable_source_snapshot(source, "project"),
            )

            self.assertEqual(inventory.call_count, 2)
            self.assertEqual(tracking.call_count, 2)

    def test_stable_source_snapshot_rejects_second_pass_binding_changes(self):
        for replacement, expected_code, recoverable in (
            ("symlink", "unsafe_legacy_source", False),
            ("special", "unsafe_legacy_source", False),
            ("missing", "source_unstable", True),
            ("regular", "source_unstable", True),
        ):
            with self.subTest(
                replacement=replacement
            ), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                source = temp / ".memory"
                child = source / "child"
                child.mkdir(parents=True)
                (child / "inside.md").write_text("inside\n", encoding="utf-8")
                outside = temp / "outside"
                outside.mkdir()
                saved_child = temp / "saved-child"
                migration = self.migration_module()
                original_inventory = migration._inventory_files
                original_open = os.open
                inventory_pass = 0
                replaced = False

                def counted_inventory(path):
                    nonlocal inventory_pass
                    inventory_pass += 1
                    return original_inventory(path)

                def replace_after_child_open(
                    path,
                    flags,
                    mode=0o777,
                    *,
                    dir_fd=None,
                ):
                    nonlocal replaced
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                    if (
                        inventory_pass == 2
                        and not replaced
                        and dir_fd is not None
                        and Path(path) == Path("child")
                        and flags & os.O_DIRECTORY
                    ):
                        child.rename(saved_child)
                        if replacement == "symlink":
                            child.symlink_to(outside, target_is_directory=True)
                        elif replacement == "special":
                            os.mkfifo(child)
                        elif replacement == "regular":
                            child.write_text("replacement\n", encoding="utf-8")
                        replaced = True
                    return descriptor

                with mock.patch.object(
                    migration,
                    "_inventory_files",
                    side_effect=counted_inventory,
                ), mock.patch.object(
                    migration.os,
                    "open",
                    side_effect=replace_after_child_open,
                ):
                    try:
                        migration._stable_source_snapshot(source, "global")
                    except LoopMemoryError as error:
                        self.assertEqual(error.code, expected_code)
                        self.assertEqual(error.recoverable, recoverable)
                    else:
                        self.fail("stable snapshot accepted a changed child binding")

                self.assertEqual(inventory_pass, 2)
                self.assertTrue(replaced)

    def test_stable_source_snapshot_returns_normalized_metadata(self):
        migration = self.migration_module()
        source = Path("/legacy/.memory")
        first_files = [
            {
                "relative_path": "long.md",
                "sha256": "a" * 64,
                "size": 1,
            },
            {
                "relative_path": "short.md",
                "sha256": "b" * 64,
                "size": 2,
            },
        ]
        second_files = [dict(record) for record in first_files]
        first_tracked = ["long.md", "short.md"]
        second_tracked = list(first_tracked)

        with mock.patch.object(
            migration,
            "_inventory_files",
            side_effect=[(first_files, True), (second_files, True)],
        ) as inventory, mock.patch.object(
            migration,
            "_observation_snapshot",
            side_effect=[(first_tracked, True), (second_tracked, True)],
        ) as tracking:
            snapshot = migration._stable_source_snapshot(source, "project")

        self.assertIsInstance(snapshot, migration.SourceSnapshot)
        expected_files = [dict(record) for record in first_files]
        expected_tracked = list(first_tracked)
        self.assertEqual(snapshot.files, expected_files)
        self.assertEqual(snapshot.catalogued_files, expected_tracked)
        self.assertIsNot(snapshot.files, first_files)
        self.assertIsNot(snapshot.catalogued_files, first_tracked)
        self.assertTrue(
            all(
                snapshot_record is not producer_record
                for snapshot_record, producer_record in zip(
                    snapshot.files,
                    first_files,
                )
            )
        )
        self.assertTrue(snapshot.has_credential_assignment)
        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(
            tracking.call_args_list,
            [mock.call(source, "project"), mock.call(source, "project")],
        )
        first_files[0]["sha256"] = "c" * 64
        first_files.append(
            {
                "relative_path": "later.md",
                "sha256": "d" * 64,
                "size": 3,
            }
        )
        first_tracked.clear()
        self.assertEqual(snapshot.files, expected_files)
        self.assertEqual(snapshot.catalogued_files, expected_tracked)
        with self.assertRaises(FrozenInstanceError):
            snapshot.has_credential_assignment = False

    def test_scan_rejects_fifo_in_legacy_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            os.mkfifo(source / "legacy.pipe")
            loop_root = temp / "loop"

            with self.assertRaises(LoopMemoryError) as context:
                self.migration_module().scan_legacy(loop_root, cwd, [])

            self.assertEqual(context.exception.code, "unsafe_legacy_source")
            manifests = loop_root / "migrations" / "manifests"
            self.assertEqual(list(manifests.glob("*.json")), [])

    def test_scan_rejects_loop_root_source_overlap_before_creating_layout(self):
        for relation in ("root_inside_source", "source_inside_root"):
            with self.subTest(relation=relation), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                if relation == "root_inside_source":
                    cwd = temp / "project"
                    source = cwd / ".memory"
                    source.mkdir(parents=True)
                    (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
                    loop_root = source / "loop"
                    candidates = []
                    self.assertFalse(loop_root.exists())
                else:
                    loop_root = temp / "loop"
                    source = loop_root / "project" / ".memory"
                    source.mkdir(parents=True)
                    (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
                    cwd = temp / "unrelated"
                    cwd.mkdir()
                    candidates = [source]

                with self.assertRaises(LoopMemoryError) as context:
                    self.migration_module().scan_legacy(
                        loop_root,
                        cwd,
                        candidates,
                    )

                self.assertEqual(context.exception.code, "unsafe_legacy_source")
                self.assertFalse((loop_root / "registry.json").exists())
                self.assertFalse((loop_root / "locks").exists())

    def test_scan_rejects_reserved_loop_root_before_creating_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text("legacy\n", encoding="utf-8")
            loop_root = (temp / "reserved-loop").resolve(strict=False)
            migration = self.migration_module()

            with mock.patch.object(
                migration,
                "is_reserved_product_path",
                side_effect=lambda path: Path(path).resolve(strict=False) == loop_root,
            ):
                with self.assertRaises(LoopMemoryError) as context:
                    migration.scan_legacy(loop_root, cwd, [])

            self.assertEqual(context.exception.code, "reserved_product_memory")
            self.assertFalse(loop_root.exists())

    def test_product_roots_are_rejected_before_resolve_or_stat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            codex_home = home / ".codex"
            product_root = codex_home / "memories"
            cwd = temp / "project"
            cwd.mkdir()
            migration = self.migration_module()
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

            operations = (
                (
                    "scan",
                    lambda: migration.scan_legacy(product_root, cwd, []),
                ),
                (
                    "apply",
                    lambda: migration.apply_migration(
                        product_root,
                        temp / "manifest.json",
                        temp / "classification.json",
                    ),
                ),
                (
                    "recover",
                    lambda: migration.recover_migration(
                        product_root,
                        temp / "manifest.json",
                    ),
                ),
                (
                    "refresh",
                    lambda: migration.refresh_migration(
                        product_root,
                        temp / "manifest.json",
                    ),
                ),
            )

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(Path, "resolve", new=guarded_resolve),
                mock.patch.object(Path, "lstat", new=guarded_lstat),
                mock.patch.object(Path, "stat", new=guarded_stat),
            ):
                for name, operation in operations:
                    with self.subTest(operation=name):
                        with self.assertRaises(LoopMemoryError) as context:
                            operation()
                        self.assertEqual(
                            context.exception.code,
                            "reserved_product_memory",
                        )

    def test_refresh_rejects_root_resolving_to_product_memory_before_manifest_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            product_root = home / ".codex" / "memories"
            product_root.mkdir(parents=True)
            lexical_root = temp / "loop-alias"
            lexical_root.symlink_to(product_root, target_is_directory=True)
            manifest_path = product_root / "must-not-be-read.json"
            migration = self.migration_module()

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(
                    migration,
                    "_safe_loop_path",
                    side_effect=AssertionError("manifest path was accessed"),
                ),
            ):
                self.assert_migration_error(
                    "reserved_product_memory",
                    False,
                    lambda: migration.refresh_migration(
                        lexical_root,
                        manifest_path,
                    ),
                )

    def test_scan_rejects_overlapping_source_from_existing_manifest_and_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            safe_cwd = temp / "safe-project"
            safe_source = safe_cwd / ".memory"
            safe_source.mkdir(parents=True)
            content = b"legacy\n"
            (safe_source / "legacy.md").write_bytes(content)
            loop_root = temp / "loop"
            migration = self.migration_module()
            scanned = migration.scan_legacy(loop_root, safe_cwd, [])
            manifest_path = Path(scanned["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            overlapping_source = loop_root / "nested" / ".memory"
            overlapping_source.mkdir(parents=True)
            (overlapping_source / "legacy.md").write_bytes(content)
            manifest["source"] = str(overlapping_source.resolve())
            self.persist_manifest(migration, loop_root, manifest_path, manifest)
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            ledger_before = ledger_path.read_bytes()
            registry_path = loop_root / "registry.json"
            registry_before = registry_path.read_bytes()
            cwd = temp / "other-project"
            cwd.mkdir()

            with self.assertRaises(LoopMemoryError) as context:
                migration.scan_legacy(loop_root, cwd, [])

            self.assertEqual(context.exception.code, "unsafe_legacy_source")
            self.assertEqual(ledger_path.read_bytes(), ledger_before)
            self.assertEqual(registry_path.read_bytes(), registry_before)

    def test_apply_and_recover_reject_manifest_source_overlapping_loop_root(self):
        for operation in ("apply", "recover"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                cwd = temp / "project"
                source = cwd / ".memory"
                source.mkdir(parents=True)
                (source / "project.md").write_text(
                    PROJECT_TEMPLATE,
                    encoding="utf-8",
                )
                loop_root = temp / "loop"
                migration = self.migration_module()
                scanned = migration.scan_legacy(loop_root, cwd, [])
                manifest_path = Path(scanned["manifests"][0])
                manifest = migration.load_manifest(manifest_path)
                overlapping_source = loop_root / "legacy" / ".memory"
                overlapping_source.mkdir(parents=True)
                overlapping_file = overlapping_source / "project.md"
                overlapping_file.write_text(PROJECT_TEMPLATE, encoding="utf-8")
                manifest["source"] = str(overlapping_source.resolve())
                self.persist_manifest(migration, loop_root, manifest_path, manifest)
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                    actions=[
                        {
                            "source": "project.md",
                            "destination": "project/project.md",
                            "mode": "copy",
                        }
                    ],
                )
                target_file = Path(manifest["target"]) / "project.md"
                manifest_before = manifest_path.read_bytes()
                source_before = overlapping_file.read_bytes()

                with self.assertRaises(LoopMemoryError) as context:
                    if operation == "apply":
                        migration.apply_migration(
                            loop_root,
                            manifest_path,
                            classification_path,
                        )
                    else:
                        migration.recover_migration(loop_root, manifest_path)

                self.assertEqual(context.exception.code, "corrupt_state")
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertEqual(overlapping_file.read_bytes(), source_before)
                self.assertFalse(target_file.exists())

    def test_scan_rejects_backslash_path_before_writing_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "unsafe\\name.md").write_text("legacy\n", encoding="utf-8")
            loop_root = temp / "loop"

            with self.assertRaises(LoopMemoryError) as context:
                self.migration_module().scan_legacy(loop_root, cwd, [])

            self.assertEqual(context.exception.code, "unsafe_legacy_source")
            manifests = loop_root / "migrations" / "manifests"
            self.assertEqual(list(manifests.glob("*.json")), [])

    def test_scan_ignores_unrelated_project_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            cwd.mkdir()
            source = cwd / ".memory"
            source.mkdir()
            (source / "project.md").write_text("# Legacy project\n", encoding="utf-8")
            metadata = cwd / ".metadata"
            metadata.mkdir()
            (metadata / "catalog.json").write_text("{}\n", encoding="utf-8")
            loop_root = temp / "loop"

            result = self.migration_module().scan_legacy(loop_root, cwd, [])

            manifest = self.migration_module().load_manifest(
                Path(result["manifests"][0])
            )
            self.assertEqual(manifest["catalogued_files"], [])
            self.assertNotIn("protected", manifest)
            self.assertEqual((metadata / "catalog.json").read_text(), "{}\n")

    def test_project_metadata_added_after_scan_does_not_change_snapshot_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            project.mkdir()
            source = project / ".memory"
            source.mkdir()
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            self.assertEqual(manifest["catalogued_files"], [])
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "project.md",
                        "destination": "project/project.md",
                        "mode": "copy",
                    }
                ],
                "reference_updates": [],
                "approved_protected": False,
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")
            (project / ".metadata").write_text("unrelated\n", encoding="utf-8")

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["catalogued_files"], [])
            self.assertNotIn("protected", completed)
            self.assertTrue(source.is_dir())
            self.assertTrue((Path(manifest["target"]) / "project.md").is_file())

    def test_project_merge_without_section_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text(
                "# Legacy\n\n## Entries\n\n- missing section must fail\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "legacy.md",
                                "destination": "project/project.md",
                                "mode": "merge_entries",
                            }
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest_before = manifest_path.read_bytes()
            source_before = (source / "legacy.md").read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(context.exception.code, "invalid_classification")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual((source / "legacy.md").read_bytes(), source_before)
            self.assertFalse(Path(manifest["target"]).exists())

    def test_project_merge_without_baseline_creates_canonical_promotable_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            imported = "- imported verified project fact"
            (source / "legacy.md").write_text(
                f"# Legacy\n\n## Entries\n\n{imported}\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
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
                ),
                encoding="utf-8",
            )

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            project_path = Path(manifest["target"]) / "project.md"
            project_text = project_path.read_text(encoding="utf-8")
            self.assertEqual(completed["state"], "complete")
            plan = json.loads(
                (
                    Path(completed["staging_path"]) / "publish-plan.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                plan["actions"][0]["sources"],
                [{"source": "legacy.md", "section": "Verified Facts"}],
            )
            self.assertEqual(
                [line[3:] for line in project_text.splitlines() if line.startswith("## ")],
                list(PROJECT_SECTIONS),
            )
            self.assertIn(imported, project_text)
            promoted = (
                "- [2026-08-10][verified] Project migration remains promotable.\n"
                "  Evidence: project merge regression\n"
            )
            self.assertTrue(
                promote_entry(
                    loop_root,
                    manifest["project_id"],
                    "project",
                    "Verified Facts",
                    promoted,
                )
            )
            self.assertIn(
                promoted.strip(),
                project_path.read_text(encoding="utf-8"),
            )
            repeated = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )
            self.assertEqual(repeated["state"], "complete")

    def test_project_merge_preserves_other_canonical_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            imported = "- imported engineering pattern"
            (source / "legacy.md").write_text(
                f"# Legacy\n\n## Entries\n\n{imported}\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            project_path = Path(manifest["target"]) / "project.md"
            project_path.parent.mkdir(parents=True)
            baseline = PROJECT_TEMPLATE.replace(
                "## Decisions\n",
                "## Decisions\n\n- preserved decision\n",
            ).replace(
                "## Risks\n",
                "## Risks\n\n- preserved risk\n",
            )
            project_path.write_text(baseline, encoding="utf-8")
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "legacy.md",
                                "destination": "project/project.md",
                                "mode": "merge_entries",
                                "section": "Engineering Patterns",
                            }
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            merged = project_path.read_text(encoding="utf-8")
            self.assertEqual(completed["state"], "complete")
            self.assertIn(imported, merged)
            self.assertIn("- preserved decision", merged)
            self.assertIn("- preserved risk", merged)

    def test_project_merge_rejects_deletion_from_unselected_candidate_section(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "legacy.md").write_text(
                "# Legacy\n\n## Entries\n\n- imported verified fact\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            project_path = Path(manifest["target"]) / "project.md"
            project_path.parent.mkdir(parents=True)
            project_path.write_text(
                PROJECT_TEMPLATE.replace(
                    "## Decisions\n",
                    "## Decisions\n\n- baseline decision must remain\n",
                ),
                encoding="utf-8",
            )
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
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
                ),
                encoding="utf-8",
            )
            paused = self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "validated",
            )
            self.assertEqual(paused["state"], "validated")
            project_path.write_text(
                project_path.read_text(encoding="utf-8").replace(
                    "- baseline decision must remain\n",
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(context.exception.code, "target_changed")
            self.assertTrue(source.is_dir())
            self.assertEqual(migration.load_manifest(manifest_path)["state"], "validated")

    def test_project_multi_section_merge_aggregates_one_target_and_remains_promotable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy Long\n\n## Entries\n\n- imported verified fact\n",
                encoding="utf-8",
            )
            (source / "medium.md").write_text(
                "# Legacy Medium\n\n## Entries\n\n- imported engineering pattern\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "long.md",
                                "destination": "project/project.md",
                                "mode": "merge_entries",
                                "section": "Verified Facts",
                            },
                            {
                                "source": "medium.md",
                                "destination": "project/project.md",
                                "mode": "merge_entries",
                                "section": "Engineering Patterns",
                            },
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            project_path = Path(manifest["target"]) / "project.md"
            project_text = project_path.read_text(encoding="utf-8")
            self.assertEqual(completed["state"], "complete")
            self.assertEqual(len(completed["target_files"]), 1)
            self.assertIn("- imported verified fact", project_text)
            self.assertIn("- imported engineering pattern", project_text)
            for section, description in (
                ("Verified Facts", "Verified promotion after aggregate merge."),
                ("Engineering Patterns", "Pattern promotion after aggregate merge."),
            ):
                with self.subTest(section=section):
                    self.assertTrue(
                        promote_entry(
                            loop_root,
                            manifest["project_id"],
                            "project",
                            section,
                            (
                                f"- [2026-08-10][verified] {description}\n"
                                f"  Evidence: aggregate {section} migration test\n"
                            ),
                        )
                    )

    def test_aggregate_project_plan_rejects_invalid_sources_and_classification_drift(self):
        for condition in (
            "empty_sources",
            "duplicate_source",
            "invalid_section",
            "classification_mismatch",
        ):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                cwd = temp / "project"
                source = cwd / ".memory"
                source.mkdir(parents=True)
                for name in ("long.md", "medium.md"):
                    (source / name).write_text(
                        "# Legacy\n\n## Entries\n\n- same aggregate block\n",
                        encoding="utf-8",
                    )
                loop_root = temp / "loop"
                migration = self.migration_module()
                result = migration.scan_legacy(loop_root, cwd, [])
                manifest_path = Path(result["manifests"][0])
                manifest = migration.load_manifest(manifest_path)
                actions = [
                    {
                        "source": "long.md",
                        "destination": "project/project.md",
                        "mode": "merge_entries",
                        "section": "Verified Facts",
                    },
                    {
                        "source": "medium.md",
                        "destination": "project/project.md",
                        "mode": "merge_entries",
                        "section": "Engineering Patterns",
                    },
                ]
                classification_path = temp / "classification.json"
                classification_path.write_text(
                    json.dumps(
                        {
                            "migration_id": manifest["migration_id"],
                            "actions": actions,
                            "reference_updates": [],
                        }
                    ),
                    encoding="utf-8",
                )
                copied = self.interrupt_after_manifest_state(
                    migration,
                    loop_root,
                    manifest_path,
                    classification_path,
                    "copied",
                )
                plan_path = Path(copied["staging_path"]) / "publish-plan.json"
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                sources = plan["actions"][0]["sources"]
                if condition == "empty_sources":
                    plan["actions"][0]["sources"] = []
                elif condition == "duplicate_source":
                    sources[1]["source"] = sources[0]["source"]
                elif condition == "invalid_section":
                    sources[0]["section"] = "Unknown"
                else:
                    sources[0]["section"], sources[1]["section"] = (
                        sources[1]["section"],
                        sources[0]["section"],
                    )
                plan_bytes = (
                    json.dumps(
                        plan,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                plan_path.write_bytes(plan_bytes)
                corrupt = dict(copied)
                corrupt["publish_plan_sha256"] = hashlib.sha256(
                    plan_bytes
                ).hexdigest()
                self.persist_manifest(migration, loop_root, manifest_path, corrupt)

                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

                self.assertEqual(context.exception.code, "corrupt_state")
                self.assertTrue(source.is_dir())
                self.assertFalse((Path(manifest["target"]) / "project.md").exists())

    def test_global_and_session_duplicate_destinations_remain_rejected(self):
        for source_kind in ("global", "session"):
            with self.subTest(source_kind=source_kind), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                cwd = temp / source_kind
                source = cwd / ".memory"
                source.mkdir(parents=True)
                migration = self.migration_module()
                if source_kind == "global":
                    (source / "long.md").write_text(
                        "# Long\n\n## Entries\n\n- long\n",
                        encoding="utf-8",
                    )
                    (source / "medium.md").write_text(
                        "# Medium\n\n## Entries\n\n- medium\n",
                        encoding="utf-8",
                    )
                    self.mock_canonical_global(migration, source)
                    actions = [
                        {
                            "source": name,
                            "destination": "global/long.md",
                            "mode": "merge_entries",
                        }
                        for name in ("long.md", "medium.md")
                    ]
                else:
                    for name in ("status.md", "handoff.md"):
                        path = source / "agents" / "codex" / "main" / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f"# Legacy {name}\n", encoding="utf-8")
                    actions = [
                        {
                            "source": f"agents/codex/main/{name}",
                            "destination": "session_archive/status.md",
                            "mode": "copy",
                        }
                        for name in ("status.md", "handoff.md")
                    ]
                loop_root = temp / "loop"
                result = migration.scan_legacy(loop_root, cwd, [])
                manifest_path = Path(result["manifests"][0])
                manifest = migration.load_manifest(manifest_path)
                classification_path = temp / "classification.json"
                classification_path.write_text(
                    json.dumps(
                        {
                            "migration_id": manifest["migration_id"],
                            "actions": actions,
                            "reference_updates": [],
                        }
                    ),
                    encoding="utf-8",
                )
                manifest_before = manifest_path.read_bytes()

                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

                self.assertEqual(context.exception.code, "invalid_classification")
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertTrue(source.is_dir())

    def test_project_copy_and_merge_cannot_be_mixed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            (source / "legacy.md").write_text(
                "# Legacy\n\n## Entries\n\n- merge entry\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "project.md",
                                "destination": "project/project.md",
                                "mode": "copy",
                            },
                            {
                                "source": "legacy.md",
                                "destination": "project/project.md",
                                "mode": "merge_entries",
                                "section": "Verified Facts",
                            },
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest_before = manifest_path.read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(context.exception.code, "invalid_classification")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertFalse(Path(manifest["target"]).exists())

    def test_noncanonical_project_copy_is_rejected_before_publish(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            source_path = source / "project.md"
            source_path.write_text("# Legacy project only\n", encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "project.md",
                                "destination": "project/project.md",
                                "mode": "copy",
                            }
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest_before = manifest_path.read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(context.exception.code, "migration_conflict")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertTrue(source.is_dir())
            self.assertFalse(Path(manifest["target"]).exists())

    def test_project_metadata_added_before_retention_does_not_block_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            project.mkdir()
            source = project / ".memory"
            source.mkdir()
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "project.md",
                        "destination": "project/project.md",
                        "mode": "copy",
                    }
                ],
                "reference_updates": [],
                "approved_protected": False,
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")
            original_ledger_event = migration._ensure_ledger_event
            interrupted = False

            def interrupt_after_references(root, migration_id, state):
                nonlocal interrupted
                if state == "references_updated" and not interrupted:
                    interrupted = True
                    raise RuntimeError("pause before quarantine")
                return original_ledger_event(root, migration_id, state)

            with mock.patch.object(
                migration,
                "_ensure_ledger_event",
                side_effect=interrupt_after_references,
            ):
                with self.assertRaisesRegex(RuntimeError, "before quarantine"):
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )
            self.assertEqual(
                migration.load_manifest(manifest_path)["state"],
                "references_updated",
            )
            (project / ".metadata").write_text("unrelated\n", encoding="utf-8")

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["catalogued_files"], [])
            self.assertNotIn("protected", completed)
            self.assertTrue(source.is_dir())

    def test_late_catalogue_observation_can_resume_when_only_protection_changes(self):
        for interrupted_state in ("copied", "references_updated"):
            with self.subTest(state=interrupted_state), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                repo = temp / "repo"
                repo.mkdir()
                source = repo / ".memory"
                source.mkdir()
                (source / "project.md").write_text(
                    PROJECT_TEMPLATE,
                    encoding="utf-8",
                )
                loop_root = temp / "loop"
                migration = self.migration_module()
                result = migration.scan_legacy(loop_root, repo, [])
                manifest_path = Path(result["manifests"][0])
                manifest = migration.load_manifest(manifest_path)
                classification = {
                    "migration_id": manifest["migration_id"],
                    "actions": [
                        {
                            "source": "project.md",
                            "destination": "project/project.md",
                            "mode": "copy",
                        }
                    ],
                    "reference_updates": [],
                    "approved_protected": False,
                }
                classification_path = temp / "classification.json"
                classification_path.write_text(
                    json.dumps(classification),
                    encoding="utf-8",
                )
                paused = self.interrupt_after_manifest_state(
                    migration,
                    loop_root,
                    manifest_path,
                    classification_path,
                    interrupted_state,
                )
                pinned_hash = paused["classification_sha256"]
                with mock.patch.object(
                    migration,
                    "_observation_snapshot",
                    return_value=(["project.md"], True),
                ):
                    completed = migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

                self.assertEqual(completed["state"], "complete")
                self.assertEqual(completed["classification_sha256"], pinned_hash)
                self.assertEqual(completed["catalogued_files"], [])
                self.assertTrue(source.is_dir())

    def test_apply_uses_custody_snapshot_without_reobserving_external_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text(PROJECT_TEMPLATE, encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "project.md",
                        "destination": "project/project.md",
                        "mode": "copy",
                    }
                ],
                "reference_updates": [],
                "approved_protected": False,
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")

            with mock.patch.object(
                migration,
                "_observation_snapshot",
                side_effect=AssertionError("apply re-observed external metadata"),
            ):
                completed = migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(completed["state"], "complete")
            self.assertTrue(source.is_dir())
            self.assertTrue(Path(manifest["target"]).exists())

    def test_project_long_memory_source_risk_is_scoped_and_not_treated_as_global(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            repo = temp / "project"
            repo.mkdir()
            source = repo / ".memory"
            source.mkdir()
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\nSERVICE_TOKEN=redacted-test-value\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()

            result = migration.scan_legacy(loop_root, repo, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "long.md",
                                "destination": "project/project.md",
                                "mode": "copy",
                            }
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(manifest["source_kind"], "project")
            self.assertEqual(manifest["catalogued_files"], [])
            self.assertIs(manifest["protected"], True)
            self.assertEqual(
                manifest["protection_reasons"],
                ["credential_assignment"],
            )
            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )
            self.assertEqual(context.exception.code, "protected_migration")
            self.assertTrue(source.is_dir())
            self.assertFalse(Path(manifest["target"]).exists())

            forged = {
                **manifest,
                "source_kind": "global",
                "project_id": None,
                "target": str((loop_root / "global").resolve()),
            }
            forged_path = temp / "forged-global.json"
            self.persist_manifest(migration, loop_root, forged_path, forged)
            with self.assertRaises(LoopMemoryError) as forged_context:
                migration.load_manifest(forged_path)
            self.assertEqual(forged_context.exception.code, "corrupt_state")

    def test_canonical_global_source_has_no_repository_derived_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            repo = temp / "codex-host"
            repo.mkdir()
            source = repo / ".memory"
            source.mkdir()
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\n- global\n",
                encoding="utf-8",
            )
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)

            result = migration.scan_legacy(temp / "loop", repo, [])
            manifest = migration.load_manifest(Path(result["manifests"][0]))

            self.assertEqual(manifest["source_kind"], "global")
            self.assertEqual(manifest["catalogued_files"], [])
            self.assertNotIn("protected", manifest)

    def test_detected_reinventory_drops_obsolete_catalogue_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            repo = temp / "project"
            repo.mkdir()
            source = repo / ".memory"
            source.mkdir()
            (source / "long.md").write_text(
                "# Project legacy long\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, repo, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            detected = migration._manifest_storage_value(
                {**manifest, "state": "detected"}, loop_root
            )
            manifest_path.write_text(
                json.dumps(detected, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            ledger_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "state": "detected",
                        "timestamp": manifest["updated_at"],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "long.md",
                                "destination": "project/project.md",
                                "mode": "copy",
                            }
                        ],
                        "reference_updates": [],
                        "approved_protected": True,
                    }
                ),
                encoding="utf-8",
            )

            reinventoried = self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "inventoried",
            )

            self.assertEqual(reinventoried["source_kind"], "project")
            self.assertEqual(reinventoried["catalogued_files"], [])
            self.assertNotIn("protected", reinventoried)
            self.assertTrue(source.is_dir())

    def test_scan_marks_credential_assignment_without_leaking_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            secret = "synthetic-value-never-return"
            (source / ".env").write_text(
                f"OPENAI_API_KEY={secret}\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"

            result = self.migration_module().scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = self.migration_module().load_manifest(manifest_path)

            self.assertIs(manifest["protected"], True)
            self.assertIn("credential_assignment", manifest["protection_reasons"])
            observable = json.dumps(result) + manifest_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, observable)

    def test_inventory_ignores_case_tokens_and_bare_key_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".memory"
            source.mkdir()
            (source / "runtime.py").write_text(
                "CASE_TOKEN = RUN_ID + '-case'\n"
                "rows = sorted(rows, key=lambda row: row['name'])\n",
                encoding="utf-8",
            )
            (source / "evidence.json").write_text(
                '{"case_token": "run-1-case", "key": "row-name"}\n',
                encoding="utf-8",
            )

            _, has_credential_assignment = self.migration_module()._inventory_files(
                source
            )

            self.assertIs(has_credential_assignment, False)

    def test_custody_keeps_conservative_historical_credential_mark(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            source = project / ".memory"
            source.mkdir(parents=True)
            (source / ".env").write_text(
                "SERVICE_TOKEN=synthetic-value\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, project, [])
            manifest = migration.load_manifest(Path(result["manifests"][0]))

            with mock.patch.object(
                migration,
                "_inventory_files",
                return_value=(manifest["files"], False),
            ):
                custody = migration._manifest_custody_snapshot(loop_root, manifest)

            self.assertIs(custody.has_credential_assignment, True)

    def test_scan_protects_broad_suspected_credentials_without_value_leakage(self):
        fixtures = {
            "token": "SERVICE_TOKEN=token-value-never-return",
            "secret": "deploy_secret: secret-value-never-return",
            "password": "DB_PASSWORD = password-value-never-return",
            "key": '"signing_key": "key-value-never-return"',
            "private_key": (
                "-----BEGIN RSA PRIVATE KEY-----\n"
                "private-key-body-never-return\n"
                "-----END RSA PRIVATE KEY-----"
            ),
            "bearer": "Authorization: Bearer bearer-value-never-return",
        }
        for name, body in fixtures.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                cwd = temp / "project"
                source = cwd / ".memory"
                source.mkdir(parents=True)
                (source / "config.txt").write_text(body + "\n", encoding="utf-8")
                loop_root = temp / "loop"

                result = self.migration_module().scan_legacy(loop_root, cwd, [])
                manifest_path = Path(result["manifests"][0])
                manifest = self.migration_module().load_manifest(manifest_path)

                self.assertIs(manifest["protected"], True)
                self.assertIn(
                    "credential_assignment",
                    manifest["protection_reasons"],
                )
                observable = (
                    json.dumps(result)
                    + manifest_path.read_text(encoding="utf-8")
                    + (loop_root / "migrations" / "ledger.jsonl").read_text(
                        encoding="utf-8"
                    )
                )
                for secret_fragment in (
                    "token-value-never-return",
                    "secret-value-never-return",
                    "password-value-never-return",
                    "key-value-never-return",
                    "private-key-body-never-return",
                    "bearer-value-never-return",
                ):
                    self.assertNotIn(secret_fragment, observable)

    def test_scan_excludes_product_memory_paths_even_when_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            codex_home = home / ".codex"
            cwd = temp / "project"
            cwd.mkdir()
            product_paths = [
                codex_home / "memories",
                codex_home / "memories_1.sqlite-wal",
                codex_home / "sqlite" / "memories_1.sqlite-shm",
            ]
            original_resolve = Path.resolve
            original_lstat = Path.lstat
            original_stat = Path.stat

            def under_product(path: Path) -> bool:
                lexical = Path(os.path.abspath(path))
                return any(
                    lexical == candidate
                    or (
                        candidate.name == "memories"
                        and lexical.is_relative_to(candidate)
                    )
                    for candidate in product_paths
                )

            def guarded_resolve(path, *args, **kwargs):
                if under_product(path):
                    raise AssertionError("product candidate was resolved")
                return original_resolve(path, *args, **kwargs)

            def guarded_lstat(path, *args, **kwargs):
                if under_product(path):
                    raise AssertionError("product candidate was lstatted")
                return original_lstat(path, *args, **kwargs)

            def guarded_stat(path, *args, **kwargs):
                if under_product(path):
                    raise AssertionError("product candidate was statted")
                return original_stat(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(Path, "resolve", new=guarded_resolve),
                mock.patch.object(Path, "lstat", new=guarded_lstat),
                mock.patch.object(Path, "stat", new=guarded_stat),
            ):
                result = self.migration_module().scan_legacy(
                    temp / "loop",
                    cwd,
                    product_paths,
                )

            self.assertEqual(result["manifests"], [])
            self.assertEqual(
                result["excluded"],
                [
                    {
                        "path": str(path),
                        "reason": "reserved_product_memory",
                    }
                    for path in product_paths
                ],
            )
            self.assertEqual(len(result["warnings"]), 1)
            self.assertNotIn("memories_1", result["warnings"][0])

    def test_load_manifest_rejects_future_schema_and_corrupt_fields_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, _, manifest = self.scan_global(temp)
            cases = (
                ("future", {**manifest, "schema_version": 3}, "unsupported_schema"),
                ("unknown", {**manifest, "future_field": True}, "corrupt_state"),
                ("relative", {**manifest, "source": ".memory"}, "corrupt_state"),
                ("state_type", {**manifest, "state": []}, "corrupt_state"),
                (
                    "source_kind_type",
                    {**manifest, "source_kind": []},
                    "corrupt_state",
                ),
                ("bad_hash", {
                    **manifest,
                    "files": [{**manifest["files"][0], "sha256": "bad"}],
                }, "corrupt_state"),
            )
            for name, value, expected_code in cases:
                with self.subTest(name=name):
                    path = temp / f"{name}.json"
                    if name == "future":
                        # Keep this case as a deliberately future raw schema.
                        stored = value
                    else:
                        # ``load_manifest`` inflates v2 internal paths for runtime
                        # callers. Persist through the storage codec so this test
                        # mutates exactly one semantic field without accidentally
                        # exercising the v2 absolute-path rejection first.
                        stored = migration._manifest_storage_value(value, temp / "loop")
                    content = json.dumps(stored, sort_keys=True).encode("utf-8") + b"\n"
                    path.write_bytes(content)

                    with self.assertRaises(LoopMemoryError) as context:
                        migration.load_manifest(path)

                    self.assertEqual(context.exception.code, expected_code)
                    self.assertEqual(path.read_bytes(), content)

    def test_classification_validation_fails_closed_before_any_migration_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            valid_action = {
                "source": "long.md",
                "destination": "global/long.md",
                "mode": "merge_entries",
            }
            cases = (
                ("mismatch", {"migration_id": f"m-{'0' * 32}"}, "classification_mismatch"),
                ("traversal", {"actions": [{**valid_action, "source": "../long.md"}]}, "invalid_classification"),
                ("mode", {"actions": [{**valid_action, "mode": "append"}]}, "invalid_classification"),
                ("types", {"actions": [{**valid_action, "source": ["long.md"]}]}, "invalid_classification"),
                ("destination", {"actions": [{**valid_action, "destination": "project/project.md"}]}, "invalid_classification"),
                ("global_section", {"actions": [{**valid_action, "section": "Entries"}]}, "invalid_classification"),
                ("references", {"reference_updates": [{"path": "AGENTS.md"}]}, "protected_reference_update"),
                ("duplicate_source", {"actions": [valid_action, {**valid_action, "destination": "global/medium.md"}]}, "invalid_classification"),
                ("unknown_field", {"future": True}, "invalid_classification"),
            )
            original_manifest = manifest_path.read_bytes()
            original_source = (source / "long.md").read_bytes()
            for name, changes, expected_code in cases:
                with self.subTest(name=name):
                    classification_path = self.write_classification(
                        temp / f"{name}.json",
                        manifest,
                        **changes,
                    )

                    with self.assertRaises(LoopMemoryError) as context:
                        migration.apply_migration(
                            loop_root,
                            manifest_path,
                            classification_path,
                        )

                    self.assertEqual(context.exception.code, expected_code)
                    self.assertEqual(manifest_path.read_bytes(), original_manifest)
                    self.assertEqual((source / "long.md").read_bytes(), original_source)
                    self.assertFalse((loop_root / "global").exists())

    def test_safe_three_field_classification_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            classification = {
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
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")

    def test_empty_reference_updates_advances_metadata_without_text_update_api(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "copy",
                    }
                ],
                "reference_updates": [],
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")
            original_write_text_atomic = migration.write_text_atomic

            def ledger_only_text_write(path, value):
                if Path(path).name != "ledger.jsonl":
                    raise AssertionError("no in-band reference text writer is defined")
                return original_write_text_atomic(path, value)

            with mock.patch.object(
                migration,
                "write_text_atomic",
                side_effect=ledger_only_text_write,
            ):
                completed = migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(completed["state"], "complete")
            ledger_states = [
                json.loads(line)["state"]
                for line in (loop_root / "migrations" / "ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(ledger_states.count("references_updated"), 1)

    def test_protected_three_field_classification_is_not_implicit_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\nOPENAI_API_KEY=hidden\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "copy",
                    }
                ],
                "reference_updates": [],
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "protected_migration")
            self.assertTrue(source.is_dir())

    def test_classification_snapshot_uses_one_safe_open_and_exact_applied_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            first_value = {
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
            changed_value = {
                **first_value,
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "copy",
                    }
                ],
            }
            classification_path = temp / "classification.json"
            first_bytes = json.dumps(first_value, sort_keys=True).encode("utf-8")
            classification_path.write_bytes(first_bytes)
            changed_bytes = json.dumps(changed_value, sort_keys=True).encode("utf-8")
            replacement = temp / "classification-replacement.json"
            replacement.write_bytes(changed_bytes)
            original_open = os.open
            classification_opens = 0

            def racing_open(path, flags, *args, **kwargs):
                nonlocal classification_opens
                descriptor = original_open(path, flags, *args, **kwargs)
                if Path(path) == classification_path:
                    classification_opens += 1
                    if classification_opens == 1:
                        replacement.replace(classification_path)
                return descriptor

            with mock.patch.object(os, "open", new=racing_open):
                snapshot = migration.load_classification_snapshot(
                    classification_path,
                    manifest,
                    loop_root.resolve(),
                )

            self.assertEqual(classification_opens, 1)
            self.assertEqual(
                json.loads(snapshot.content)["actions"][0]["mode"],
                "merge_entries",
            )
            self.assertEqual(
                json.loads(classification_path.read_text(encoding="utf-8"))[
                    "actions"
                ][0]["mode"],
                "copy",
            )
            semantic_bytes = (
                json.dumps(
                    first_value,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            digest = hashlib.sha256(semantic_bytes).hexdigest()
            self.assertEqual(snapshot.sha256, digest)

            result = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                classification_snapshot=snapshot,
            )

            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["classification_sha256"], digest)

            duplicate_path = temp / "duplicate.json"
            duplicate_path.write_text(
                "{"
                f'"migration_id":"{manifest["migration_id"]}",'
                f'"migration_id":"{manifest["migration_id"]}",'
                '"actions":[],"reference_updates":[]}',
                encoding="utf-8",
            )
            with self.assertRaises(LoopMemoryError) as context:
                migration.load_classification_snapshot(
                    duplicate_path,
                    manifest,
                    loop_root.resolve(),
                )
            self.assertEqual(context.exception.code, "invalid_classification")

    def test_staging_never_reads_transient_external_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                actions=[
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "copy",
                    }
                ],
            )
            source_path = (source / "long.md").resolve()
            inventoried = source_path.read_bytes()
            transient = b"# Transient un-inventoried content\n"
            original_read_bytes = Path.read_bytes
            source_reads = 0

            def transient_swap(path):
                nonlocal source_reads
                if path == source_path:
                    source_reads += 1
                    if source_reads == 1:
                        path.write_bytes(transient)
                        try:
                            return original_read_bytes(path)
                        finally:
                            path.write_bytes(inventoried)
                return original_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", new=transient_swap):
                completed = migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(source_reads, 0)
            self.assertEqual(source_path.read_bytes(), inventoried)
            self.assertEqual(
                (loop_root / "global" / "long.md").read_bytes(),
                inventoried,
            )

    def test_protected_manifest_requires_explicit_classification_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\n- OPENAI_API_KEY=synthetic-secret\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            source_before = (source / "long.md").read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(context.exception.code, "protected_migration")
            self.assertEqual((source / "long.md").read_bytes(), source_before)

    def test_global_merge_and_copy_complete_idempotently_with_ledger_and_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy Long\n\n## Entries\n\n- existing\n\n- imported\n  detail\n",
                encoding="utf-8",
            )
            short_body = b"# Copied Short\n\n## Entries\n\n- short legacy\n"
            (source / "short.md").write_bytes(short_body)
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            global_dir = loop_root / "global"
            global_dir.mkdir()
            (global_dir / "long.md").write_text(
                "# Global Long-Term Memory\n\n## Entries\n\n- existing\n",
                encoding="utf-8",
            )
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "merge_entries",
                    },
                    {
                        "source": "short.md",
                        "destination": "global/short.md",
                        "mode": "copy",
                    },
                ],
                "reference_updates": [],
                "approved_protected": False,
            }
            classification_path = temp / "classification.json"
            classification_bytes = (
                json.dumps(classification, sort_keys=True).encode("utf-8") + b"\n"
            )
            classification_path.write_bytes(classification_bytes)

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertTrue(source.is_dir())
            quarantine_source = Path(completed["snapshot"])
            self.assertTrue(quarantine_source.is_dir())
            merged = (global_dir / "long.md").read_text(encoding="utf-8")
            self.assertEqual(merged.count("- existing\n"), 1)
            self.assertIn("- imported\n  detail\n", merged)
            self.assertEqual((global_dir / "short.md").read_bytes(), short_body)
            semantic_classification = {
                key: classification[key]
                for key in ("migration_id", "actions", "reference_updates")
            }
            semantic_bytes = (
                json.dumps(
                    semantic_classification,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            self.assertEqual(
                completed["classification_sha256"],
                hashlib.sha256(semantic_bytes).hexdigest(),
            )
            self.assertEqual(
                [record["relative_path"] for record in completed["target_files"]],
                ["global/long.md", "global/short.md"],
            )
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            self.assertEqual(
                [event["state"] for event in ledger],
                [
                    "inventoried",
                    "copied",
                    "validated",
                    "references_updated",
                    "quarantined",
                    "complete",
                ],
            )
            self.assertTrue(
                all(
                    set(event) == {"migration_id", "state", "timestamp"}
                    and event["migration_id"] == manifest["migration_id"]
                    for event in ledger
                )
            )
            self.assertEqual(
                RegistryStore(loop_root).resolve_legacy_alias(source),
                {
                    "target": manifest["target"],
                    "migration_id": manifest["migration_id"],
                },
            )
            snapshots = {
                "manifest": manifest_path.read_bytes(),
                "ledger": ledger_path.read_bytes(),
                "long": (global_dir / "long.md").read_bytes(),
                "short": (global_dir / "short.md").read_bytes(),
            }

            repeated = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(repeated, completed)
            self.assertEqual(manifest_path.read_bytes(), snapshots["manifest"])
            self.assertEqual(ledger_path.read_bytes(), snapshots["ledger"])
            self.assertEqual((global_dir / "long.md").read_bytes(), snapshots["long"])
            self.assertEqual((global_dir / "short.md").read_bytes(), snapshots["short"])
            self.assertEqual(len(list(quarantine_source.parent.iterdir())), 2)

    def test_staging_preflight_late_action_conflict_leaves_all_final_targets_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\n- staged import\n",
                encoding="utf-8",
            )
            (source / "short.md").write_bytes(b"candidate short\n")
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            global_dir = loop_root / "global"
            global_dir.mkdir()
            long_baseline = b"# Global Long-Term Memory\n\n## Entries\n\n- baseline\n"
            short_conflict = b"existing conflicting short\n"
            (global_dir / "long.md").write_bytes(long_baseline)
            (global_dir / "short.md").write_bytes(short_conflict)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "merge_entries",
                    },
                    {
                        "source": "short.md",
                        "destination": "global/short.md",
                        "mode": "copy",
                    },
                ],
                "reference_updates": [],
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "migration_conflict")
            self.assertEqual((global_dir / "long.md").read_bytes(), long_baseline)
            self.assertEqual((global_dir / "short.md").read_bytes(), short_conflict)
            self.assertEqual(migration.load_manifest(manifest_path)["state"], "inventoried")

    def test_copied_state_keeps_baseline_and_has_complete_staging_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- staged entry\n",
            )
            global_dir = loop_root / "global"
            global_dir.mkdir()
            target = global_dir / "long.md"
            baseline = b"# Global Long-Term Memory\n\n## Entries\n\n- baseline\n"
            target.write_bytes(baseline)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )

            copied = self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )

            self.assertEqual(copied["state"], "copied")
            self.assertEqual(target.read_bytes(), baseline)
            staging = Path(copied["staging_path"])
            plan_path = staging / "publish-plan.json"
            plan_bytes = plan_path.read_bytes()
            self.assertEqual(
                copied["publish_plan_sha256"],
                hashlib.sha256(plan_bytes).hexdigest(),
            )
            plan = json.loads(plan_bytes)
            self.assertEqual(plan["migration_id"], manifest["migration_id"])
            self.assertEqual(len(plan["actions"]), 1)
            action = plan["actions"][0]
            self.assertEqual(action["baseline"]["sha256"], hashlib.sha256(baseline).hexdigest())
            candidate = staging / action["candidate"]["relative_path"]
            candidate_bytes = candidate.read_bytes()
            self.assertEqual(action["candidate"]["size"], len(candidate_bytes))
            self.assertEqual(
                action["candidate"]["sha256"],
                hashlib.sha256(candidate_bytes).hexdigest(),
            )
            self.assertIn(b"- staged entry", candidate_bytes)

    def test_partial_publish_failure_retries_mixed_baseline_candidate_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\n- import one\n",
                encoding="utf-8",
            )
            short_body = b"short candidate\n"
            (source / "short.md").write_bytes(short_body)
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            global_dir = loop_root / "global"
            global_dir.mkdir()
            long_target = global_dir / "long.md"
            long_baseline = b"# Global Long-Term Memory\n\n## Entries\n\n- baseline\n"
            long_target.write_bytes(long_baseline)
            short_target = global_dir / "short.md"
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "merge_entries",
                    },
                    {
                        "source": "short.md",
                        "destination": "global/short.md",
                        "mode": "copy",
                    },
                ],
                "reference_updates": [],
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")
            copied = self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )
            original_publish = migration._publish_candidate
            publish_count = 0

            def fail_after_first(*args, **kwargs):
                nonlocal publish_count
                original_publish(*args, **kwargs)
                publish_count += 1
                if publish_count == 1:
                    raise RuntimeError("publish interruption")

            with mock.patch.object(
                migration,
                "_publish_candidate",
                side_effect=fail_after_first,
            ):
                with self.assertRaisesRegex(RuntimeError, "publish interruption"):
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(migration.load_manifest(manifest_path)["state"], "copied")
            plan = json.loads((Path(copied["staging_path"]) / "publish-plan.json").read_text())
            first_action = plan["actions"][0]
            first_candidate = (
                Path(copied["staging_path"])
                / first_action["candidate"]["relative_path"]
            ).read_bytes()
            self.assertEqual(long_target.read_bytes(), first_candidate)
            self.assertFalse(short_target.exists())

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(short_target.read_bytes(), short_body)

    def test_publish_preflight_target_drift_causes_zero_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Legacy\n\n## Entries\n\n- import one\n",
                encoding="utf-8",
            )
            (source / "short.md").write_bytes(b"short candidate\n")
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            global_dir = loop_root / "global"
            global_dir.mkdir()
            long_target = global_dir / "long.md"
            long_baseline = b"# Global Long-Term Memory\n\n## Entries\n\n- baseline\n"
            long_target.write_bytes(long_baseline)
            short_target = global_dir / "short.md"
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "merge_entries",
                    },
                    {
                        "source": "short.md",
                        "destination": "global/short.md",
                        "mode": "copy",
                    },
                ],
                "reference_updates": [],
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")
            self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )
            drift = b"third state drift\n"
            short_target.write_bytes(drift)

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "target_changed")
            self.assertEqual(long_target.read_bytes(), long_baseline)
            self.assertEqual(short_target.read_bytes(), drift)
            self.assertEqual(migration.load_manifest(manifest_path)["state"], "copied")

    def test_held_global_migration_allows_later_promotion_and_preserves_both_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- imported legacy entry\n",
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            promoted_entry = (
                "- [2026-08-10][verified] Promotion after migration hold survives.\n"
                "  Evidence: migration staging integration test\n"
            )

            self.assertEqual(held["state"], "validated")
            self.assertTrue(
                promote_entry(
                    loop_root,
                    "p-project",
                    "global-long",
                    "Methodology",
                    promoted_entry,
                )
            )

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            final_text = (loop_root / "global" / "long.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(completed["state"], "complete")
            self.assertTrue(Path(completed["staging_path"]).is_dir())
            self.assertIn("- imported legacy entry", final_text)
            self.assertIn(promoted_entry.strip(), final_text)

    def test_staging_and_publish_plan_corruption_fail_closed(self):
        for condition in (
            "staging_path",
            "plan_hash",
            "plan_target_path",
            "candidate_symlink",
        ):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp,
                    body="- staged import\n",
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                )
                copied = self.interrupt_after_manifest_state(
                    migration,
                    loop_root,
                    manifest_path,
                    classification_path,
                    "copied",
                )
                staging = Path(copied["staging_path"])
                plan_path = staging / "publish-plan.json"
                if condition == "staging_path":
                    corrupt = dict(copied)
                    corrupt["staging_path"] = str(
                        loop_root
                        / "outside"
                        / "migrations"
                        / "staging"
                        / manifest["migration_id"]
                    )
                    self.persist_manifest(
                        migration,
                        loop_root,
                        manifest_path,
                        corrupt,
                    )
                elif condition == "plan_hash":
                    plan_path.write_bytes(plan_path.read_bytes() + b" ")
                elif condition == "plan_target_path":
                    plan = json.loads(plan_path.read_text(encoding="utf-8"))
                    plan["actions"][0]["target_relative_path"] = "../escape.md"
                    plan_bytes = (
                        json.dumps(
                            plan,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    plan_path.write_bytes(plan_bytes)
                    corrupt = dict(copied)
                    corrupt["publish_plan_sha256"] = hashlib.sha256(
                        plan_bytes
                    ).hexdigest()
                    self.persist_manifest(migration, loop_root, manifest_path, corrupt)
                else:
                    plan = json.loads(plan_path.read_text(encoding="utf-8"))
                    candidate = staging / plan["actions"][0]["candidate"]["relative_path"]
                    candidate.unlink()
                    candidate.symlink_to(source / "long.md")

                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

                self.assertIn(context.exception.code, {"corrupt_state", "unsafe_path"})
                self.assertTrue(source.is_dir())
                self.assertFalse((loop_root / "global" / "long.md").exists())

    def test_staged_candidate_drift_after_preflight_is_not_published(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- immutable staged import\n",
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            copied = self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )
            staging = Path(copied["staging_path"])
            plan = json.loads(
                (staging / "publish-plan.json").read_text(encoding="utf-8")
            )
            candidate = staging / plan["actions"][0]["candidate"]["relative_path"]
            original_publish_state = migration._publish_state

            def drift_after_preflight(*args, **kwargs):
                state = original_publish_state(*args, **kwargs)
                candidate.write_bytes(b"drift after complete preflight\n")
                return state

            with mock.patch.object(
                migration,
                "_publish_state",
                side_effect=drift_after_preflight,
            ):
                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertFalse((loop_root / "global" / "long.md").exists())
            self.assertEqual(
                migration.load_manifest(manifest_path)["state"],
                "copied",
            )

    def test_validated_merge_requires_imported_blocks_but_allows_later_entries(self):
        for condition in ("deleted", "modified", "appended"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp,
                    body="- exact imported block\n  continuation\n",
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                )
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                    stop_after="validated",
                )
                target = loop_root / "global" / "long.md"
                text = target.read_text(encoding="utf-8")
                if condition == "deleted":
                    text = text.replace(
                        "- exact imported block\n  continuation\n",
                        "",
                    )
                elif condition == "modified":
                    text = text.replace("exact imported block", "changed imported block")
                else:
                    text += "\n- later valid entry\n  independent continuation\n"
                target.write_text(text, encoding="utf-8")

                if condition in ("deleted", "modified"):
                    with self.assertRaises(LoopMemoryError) as context:
                        migration.apply_migration(
                            loop_root,
                            manifest_path,
                            classification_path,
                        )
                    self.assertEqual(context.exception.code, "target_changed")
                    self.assertTrue(source.is_dir())
                    self.assertEqual(
                        migration.load_manifest(manifest_path)["state"],
                        "validated",
                    )
                else:
                    completed = migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )
                    self.assertEqual(completed["state"], "complete")
                    final_text = target.read_text(encoding="utf-8")
                    self.assertIn("- exact imported block", final_text)
                    self.assertIn("- later valid entry", final_text)

    def test_live_promotion_lease_blocks_publish_without_changing_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- blocked publish\n",
            )
            target = loop_root / "global" / "long.md"
            target.parent.mkdir()
            baseline = b"# Global Long-Term Memory\n\n## Entries\n\n- baseline\n"
            target.write_bytes(baseline)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )
            promotion_lease = loop_root / "locks" / "promote-global-long.lock"

            with FileLease(promotion_lease, owner="live-promotion"):
                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(context.exception.code, "lease_busy")
            self.assertEqual(target.read_bytes(), baseline)
            self.assertEqual(
                migration.load_manifest(manifest_path)["state"],
                "copied",
            )

    def test_merge_plan_requires_source_and_candidate_blocks_already_in_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                temp,
                body="- source block already present\n",
            )
            target = loop_root / "global" / "long.md"
            target.parent.mkdir()
            target.write_text(
                "# Global Long-Term Memory\n\n## Entries\n\n"
                "- baseline block\n\n- source block already present\n",
                encoding="utf-8",
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )

            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            plan = json.loads(
                (
                    Path(held["staging_path"]) / "publish-plan.json"
                ).read_text(encoding="utf-8")
            )
            action = plan["actions"][0]
            source_hash = hashlib.sha256(
                b"- source block already present"
            ).hexdigest()
            baseline_hash = hashlib.sha256(b"- baseline block").hexdigest()
            self.assertEqual(action["source_entry_sha256"], [source_hash])
            self.assertEqual(
                set(action["candidate_entry_sha256"]),
                {source_hash, baseline_hash},
            )

            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    "- source block already present\n",
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(context.exception.code, "target_changed")
            self.assertTrue(source.is_dir())
            self.assertEqual(migration.load_manifest(manifest_path)["state"], "validated")

    def test_copied_merge_candidate_with_later_promotion_forwards_to_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- imported before copied interruption\n",
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )
            original_write_manifest = migration.write_json_atomic
            interrupted = False

            def interrupt_validated_manifest(path, value):
                nonlocal interrupted
                if value.get("state") == "validated" and not interrupted:
                    interrupted = True
                    raise RuntimeError("crash before validated manifest write")
                return original_write_manifest(path, value)

            with mock.patch.object(
                migration,
                "write_json_atomic",
                side_effect=interrupt_validated_manifest,
            ):
                with self.assertRaisesRegex(RuntimeError, "validated manifest"):
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(migration.load_manifest(manifest_path)["state"], "copied")
            promoted_entry = (
                "- [2026-08-10][verified] Promotion survived copied recovery.\n"
                "  Evidence: copied semantic superset regression\n"
            )
            self.assertTrue(
                promote_entry(
                    loop_root,
                    "p-project",
                    "global-long",
                    "Methodology",
                    promoted_entry,
                )
            )

            recovered = migration.recover_migration(loop_root, manifest_path)
            self.assertEqual(recovered["state"], "copied")
            self.assertEqual(recovered["recovery"], "consistent")

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            final_text = (loop_root / "global" / "long.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(completed["state"], "complete")
            self.assertIn("- imported before copied interruption", final_text)
            self.assertIn(promoted_entry.strip(), final_text)

    def test_publish_rechecks_baseline_and_never_overwrites_immediate_third_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(
                temp,
                body="- candidate import\n",
            )
            target = loop_root / "global" / "long.md"
            target.parent.mkdir()
            baseline = b"# Global Long-Term Memory\n\n## Entries\n\n- baseline\n"
            third_state = b"# Global Long-Term Memory\n\n## Entries\n\n- concurrent third state\n"
            target.write_bytes(baseline)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "copied",
            )
            original_publish_state = migration._publish_state
            state_checks = 0

            def change_after_full_preflight(*args, **kwargs):
                nonlocal state_checks
                state_checks += 1
                state = original_publish_state(*args, **kwargs)
                if state_checks == 1:
                    target.write_bytes(third_state)
                return state

            with mock.patch.object(
                migration,
                "_publish_state",
                side_effect=change_after_full_preflight,
            ):
                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(context.exception.code, "target_changed")
            self.assertGreaterEqual(state_checks, 2)
            self.assertEqual(target.read_bytes(), third_state)
            self.assertEqual(migration.load_manifest(manifest_path)["state"], "copied")

    def test_global_stop_after_validated_holds_source_then_explicit_resume_completes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                temp,
                body="- hold entry\n",
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )

            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )

            self.assertEqual(held["state"], "validated")
            self.assertEqual(held["hold_reason"], "governance_switch")
            self.assertTrue(source.is_dir())
            held_again = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            self.assertEqual(held_again, held)
            self.assertTrue(source.is_dir())
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            self.assertEqual(
                [json.loads(line)["state"] for line in ledger_path.read_text().splitlines()],
                ["inventoried", "copied", "validated"],
            )

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertNotIn("hold_reason", completed)
            self.assertTrue(source.is_dir())

    def test_global_nonmemory_file_can_be_retained_only_in_quarantine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            (source / "README.md").write_text("# Legacy memory guide\n", encoding="utf-8")
            for name in ("long", "medium", "short"):
                (source / f"{name}.md").write_text(
                    f"# Legacy {name.title()}\n\n## Entries\n\n- {name} entry\n",
                    encoding="utf-8",
                )
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "README.md",
                                "destination": "quarantine_only",
                                "mode": "quarantine_only",
                            },
                            *[
                                {
                                    "source": f"{name}.md",
                                    "destination": f"global/{name}.md",
                                    "mode": "merge_entries",
                                }
                                for name in ("long", "medium", "short")
                            ],
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )

            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )

            self.assertEqual(held["state"], "validated")
            self.assertTrue(source.is_dir())
            self.assertFalse((loop_root / "global/README.md").exists())
            for name in ("long", "medium", "short"):
                self.assertTrue((loop_root / f"global/{name}.md").is_file())

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            quarantined = Path(completed["snapshot"]) / "README.md"
            self.assertEqual(
                quarantined.read_text(encoding="utf-8"),
                "# Legacy memory guide\n",
            )
            self.assertEqual(
                (source / "README.md").read_text(encoding="utf-8"),
                "# Legacy memory guide\n",
            )

    def test_mixed_project_memory_routes_project_sessions_and_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            source = project / ".memory"
            (source / "project").mkdir(parents=True)
            (source / "sessions/active/legacy").mkdir(parents=True)
            (source / "agents/codex/main").mkdir(parents=True)
            (source / "evidence/run").mkdir(parents=True)
            (source / "project/long.md").write_text(
                "# Long\n\n## Entries\n\n"
                "- [2026-08-14][verified] Stable project fact.\n",
                encoding="utf-8",
            )
            (source / "project/medium.md").write_text(
                "# Medium\n\n## Entries\n\n"
                "- [2026-08-14][verified] Active project decision.\n",
                encoding="utf-8",
            )
            (source / "project/short.md").write_text(
                "# Short\n\n## Entries\n\n"
                "- [2026-08-14][verified] Current project risk.\n",
                encoding="utf-8",
            )
            (source / "sessions/active/legacy/status.md").write_text(
                "# Legacy status\n\nCASE_TOKEN = RUN_ID + '-case'\n",
                encoding="utf-8",
            )
            (source / "sessions/active/legacy/handoff.md").write_text(
                "# Legacy handoff\n",
                encoding="utf-8",
            )
            (source / "agents/codex/main/outbox.md").write_text(
                "# Legacy outbox\n",
                encoding="utf-8",
            )
            (source / "evidence/run/result.json").write_text(
                '{"ok": true}\n', encoding="utf-8"
            )
            (source / ".DS_Store").write_bytes(b"\xff\x00legacy")

            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            archive = (
                "session_archive/sessions/archive/2026-08/"
                f"s-legacy-{manifest['migration_id'][2:]}"
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                approved_protected=True,
                actions=[
                    {
                        "source": "project/long.md",
                        "destination": "project/project.md",
                        "mode": "merge_entries",
                        "section": "Verified Facts",
                    },
                    {
                        "source": "project/medium.md",
                        "destination": "project/project.md",
                        "mode": "merge_entries",
                        "section": "Decisions",
                    },
                    {
                        "source": "project/short.md",
                        "destination": "project/project.md",
                        "mode": "merge_entries",
                        "section": "Risks",
                    },
                    *[
                        {
                            "source": relative,
                            "destination": f"{archive}/{relative}",
                            "mode": "copy",
                        }
                        for relative in (
                            "sessions/active/legacy/status.md",
                            "sessions/active/legacy/handoff.md",
                            "agents/codex/main/outbox.md",
                        )
                    ],
                    *[
                        {
                            "source": relative,
                            "destination": "quarantine_only",
                            "mode": "quarantine_only",
                        }
                        for relative in ("evidence/run/result.json", ".DS_Store")
                    ],
                ],
            )

            completed = migration.apply_migration(
                loop_root, manifest_path, classification_path
            )

            self.assertEqual(completed["state"], "complete")
            project_memory = Path(manifest["target"]) / "project.md"
            project_text = project_memory.read_text(encoding="utf-8")
            self.assertIn("Stable project fact.", project_text)
            self.assertIn("Active project decision.", project_text)
            self.assertIn("Current project risk.", project_text)
            archive_root = Path(manifest["target"]) / archive.removeprefix(
                "session_archive/"
            )
            self.assertEqual(
                (archive_root / "sessions/active/legacy/status.md").read_text(
                    encoding="utf-8"
                ),
                "# Legacy status\n\nCASE_TOKEN = RUN_ID + '-case'\n",
            )
            self.assertEqual(
                (archive_root / "agents/codex/main/outbox.md").read_text(
                    encoding="utf-8"
                ),
                "# Legacy outbox\n",
            )
            self.assertFalse((Path(manifest["target"]) / "evidence").exists())
            self.assertEqual((source / ".DS_Store").read_bytes(), b"\xff\x00legacy")
            self.assertEqual(
                (Path(completed["snapshot"]) / "evidence/run/result.json").read_text(
                    encoding="utf-8"
                ),
                '{"ok": true}\n',
            )

    def test_mixed_project_routing_rejects_authority_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            source = project / ".memory/project"
            source.mkdir(parents=True)
            (source / "long.md").write_text(
                "# Long\n\n## Entries\n\n- [2026-08-14][verified] Fact.\n",
                encoding="utf-8",
            )
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                actions=[
                    {
                        "source": "project/long.md",
                        "destination": "quarantine_only",
                        "mode": "quarantine_only",
                    }
                ],
            )

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root, manifest_path, classification_path
                )

            self.assertEqual(context.exception.code, "invalid_classification")

    def test_mixed_project_routing_rejects_archive_outside_archive_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            source = project / ".memory/sessions"
            source.mkdir(parents=True)
            (source / "status.md").write_text("# Status\n", encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                actions=[
                    {
                        "source": "sessions/status.md",
                        "destination": "session_archive/project.md",
                        "mode": "copy",
                    }
                ],
            )

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(
                    loop_root, manifest_path, classification_path
                )

            self.assertEqual(context.exception.code, "invalid_classification")

    def test_global_quarantine_only_rejects_authority_swaps(self):
        cases = {
            "canonical_memory_quarantined": [
                {
                    "source": "README.md",
                    "destination": "global/long.md",
                    "mode": "merge_entries",
                },
                {
                    "source": "long.md",
                    "destination": "quarantine_only",
                    "mode": "quarantine_only",
                },
                {
                    "source": "medium.md",
                    "destination": "global/medium.md",
                    "mode": "merge_entries",
                },
                {
                    "source": "short.md",
                    "destination": "global/short.md",
                    "mode": "merge_entries",
                },
            ],
            "canonical_targets_swapped": [
                {
                    "source": "README.md",
                    "destination": "quarantine_only",
                    "mode": "quarantine_only",
                },
                {
                    "source": "long.md",
                    "destination": "global/medium.md",
                    "mode": "merge_entries",
                },
                {
                    "source": "medium.md",
                    "destination": "global/long.md",
                    "mode": "merge_entries",
                },
                {
                    "source": "short.md",
                    "destination": "global/short.md",
                    "mode": "merge_entries",
                },
            ],
            "canonical_memory_copied": [
                {
                    "source": "README.md",
                    "destination": "quarantine_only",
                    "mode": "quarantine_only",
                },
                {
                    "source": "long.md",
                    "destination": "global/long.md",
                    "mode": "copy",
                },
                {
                    "source": "medium.md",
                    "destination": "global/medium.md",
                    "mode": "merge_entries",
                },
                {
                    "source": "short.md",
                    "destination": "global/short.md",
                    "mode": "merge_entries",
                },
            ],
            "readme_published_without_quarantine": [
                {
                    "source": "README.md",
                    "destination": "global/medium.md",
                    "mode": "merge_entries",
                },
                {
                    "source": "long.md",
                    "destination": "global/long.md",
                    "mode": "merge_entries",
                },
            ],
            "long_only_quarantined": [
                {
                    "source": "long.md",
                    "destination": "quarantine_only",
                    "mode": "quarantine_only",
                },
            ],
        }
        for name, actions in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                cwd = temp / "home"
                source = cwd / ".memory"
                source.mkdir(parents=True)
                source_names = sorted(
                    {Path(action["source"]).stem for action in actions}
                )
                for file_name in source_names:
                    (source / f"{file_name}.md").write_text(
                        f"# Legacy {file_name}\n\n## Entries\n\n- {file_name} entry\n",
                        encoding="utf-8",
                    )
                loop_root = temp / "loop"
                migration = self.migration_module()
                self.mock_canonical_global(migration, source)
                result = migration.scan_legacy(loop_root, cwd, [])
                manifest_path = Path(result["manifests"][0])
                manifest = migration.load_manifest(manifest_path)
                classification_path = temp / "classification.json"
                classification_path.write_text(
                    json.dumps(
                        {
                            "migration_id": manifest["migration_id"],
                            "actions": actions,
                            "reference_updates": [],
                        }
                    ),
                    encoding="utf-8",
                )

                with self.assertRaises(LoopMemoryError) as context:
                    migration.load_classification_snapshot(
                        classification_path,
                        manifest,
                        loop_root.resolve(),
                    )

                self.assertEqual(context.exception.code, "invalid_classification")

    def test_recovery_rejects_quarantine_only_authority_swap_in_publish_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "home"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            shared = "# Legacy\n\n## Entries\n\n- shared entry\n"
            (source / "README.md").write_text(shared, encoding="utf-8")
            (source / "long.md").write_text(shared, encoding="utf-8")
            for name in ("medium", "short"):
                (source / f"{name}.md").write_text(
                    f"# Legacy {name.title()}\n\n## Entries\n\n- {name} entry\n",
                    encoding="utf-8",
                )
            loop_root = temp / "loop"
            migration = self.migration_module()
            self.mock_canonical_global(migration, source)
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification_path = temp / "classification.json"
            classification_path.write_text(
                json.dumps(
                    {
                        "migration_id": manifest["migration_id"],
                        "actions": [
                            {
                                "source": "README.md",
                                "destination": "quarantine_only",
                                "mode": "quarantine_only",
                            },
                            *[
                                {
                                    "source": f"{name}.md",
                                    "destination": f"global/{name}.md",
                                    "mode": "merge_entries",
                                }
                                for name in ("long", "medium", "short")
                            ],
                        ],
                        "reference_updates": [],
                    }
                ),
                encoding="utf-8",
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            plan_path = Path(held["staging_path"]) / "publish-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            quarantine = next(
                action
                for action in plan["actions"]
                if action["mode"] == "quarantine_only"
            )
            canonical = next(
                action
                for action in plan["actions"]
                if action["destination"] == "global/long.md"
            )
            quarantine["source"], canonical["source"] = (
                canonical["source"],
                quarantine["source"],
            )
            plan_bytes = (
                json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            plan_path.write_bytes(plan_bytes)
            corrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
            corrupted["publish_plan_sha256"] = hashlib.sha256(plan_bytes).hexdigest()
            manifest_path.write_text(
                json.dumps(corrupted, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(LoopMemoryError) as context:
                migration.recover_migration(loop_root, manifest_path)

            self.assertEqual(context.exception.code, "corrupt_state")

    def test_empty_discard_quarantines_source_without_project_knowledge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            source.mkdir(parents=True)
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": ".",
                        "destination": "discard_empty",
                        "mode": "discard_empty",
                    }
                ],
                "reference_updates": [],
                "approved_protected": False,
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["target_files"], [])
            self.assertTrue(source.is_dir())
            self.assertFalse((loop_root / "projects").exists())
            self.assertTrue(Path(completed["quarantine_path"]).is_dir())
            self.assertEqual(list(Path(completed["quarantine_path"]).iterdir()), [])

    def test_session_status_copies_to_deterministic_archived_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cwd = temp / "project"
            source = cwd / ".memory"
            status = source / "agents" / "codex" / "main" / "status.md"
            status.parent.mkdir(parents=True)
            body = b"# Legacy Session Status\n\nReady to hand off.\n"
            status.write_bytes(body)
            loop_root = temp / "loop"
            migration = self.migration_module()
            result = migration.scan_legacy(loop_root, cwd, [])
            manifest_path = Path(result["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            classification = {
                "migration_id": manifest["migration_id"],
                "actions": [
                    {
                        "source": "agents/codex/main/status.md",
                        "destination": "session_archive/status.md",
                        "mode": "copy",
                    }
                ],
                "reference_updates": [],
                "approved_protected": False,
            }
            classification_path = temp / "classification.json"
            classification_path.write_text(json.dumps(classification), encoding="utf-8")

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            archived_status = Path(manifest["target"]) / "status.md"
            self.assertEqual(completed["state"], "complete")
            self.assertEqual(archived_status.read_bytes(), body)
            self.assertEqual(
                completed["target_files"][0]["relative_path"],
                archived_status.relative_to(loop_root.resolve()).as_posix(),
            )
            project_dir = (
                loop_root / "projects" / manifest["project_id"]
            ).resolve()
            self.assertTrue((project_dir / "project.md").is_file())
            self.assertTrue((project_dir / "sessions" / "active").is_dir())
            self.assertTrue((project_dir / "sessions" / "archive").is_dir())
            self.assertTrue(
                Path(manifest["target"]).is_relative_to(
                    project_dir / "sessions" / "archive"
                )
            )

    def test_existing_copy_target_conflict_leaves_source_and_manifest_inventoried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            global_dir = loop_root / "global"
            global_dir.mkdir()
            existing = b"different existing target\n"
            (global_dir / "long.md").write_bytes(existing)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                actions=[
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "copy",
                    }
                ],
            )
            manifest_before = manifest_path.read_bytes()
            source_before = (source / "long.md").read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "migration_conflict")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual((source / "long.md").read_bytes(), source_before)
            self.assertEqual((global_dir / "long.md").read_bytes(), existing)

    def test_external_source_drift_uses_snapshot_and_classification_drift_fails_after_hold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            drift_temp = temp / "source-drift"
            drift_temp.mkdir()
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                drift_temp
            )
            classification_path = self.write_classification(
                drift_temp / "classification.json",
                manifest,
            )
            (source / "long.md").write_text("changed after scan\n", encoding="utf-8")
            snapshot_body = (Path(manifest["snapshot"]) / "long.md").read_bytes()

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            target_text = (loop_root / "global" / "long.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("- legacy", target_text)
            self.assertNotIn("changed after scan", target_text)
            self.assertIn(b"- legacy", snapshot_body)
            self.assertEqual(
                (source / "long.md").read_text(encoding="utf-8"),
                "changed after scan\n",
            )

            pin_temp = temp / "classification-drift"
            pin_temp.mkdir()
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                pin_temp
            )
            classification_path = self.write_classification(
                pin_temp / "classification.json",
                manifest,
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            held_bytes = manifest_path.read_bytes()
            changed_classification = json.loads(
                classification_path.read_text(encoding="utf-8")
            )
            changed_classification["actions"][0]["mode"] = "copy"
            classification_path.write_text(
                json.dumps(changed_classification),
                encoding="utf-8",
            )

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "classification_mismatch")
            self.assertEqual(manifest_path.read_bytes(), held_bytes)
            self.assertEqual(held["state"], "validated")
            self.assertTrue(source.is_dir())

    def test_corrupt_ledger_and_live_migration_lease_fail_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            manifest_before = manifest_path.read_bytes()
            source_before = (source / "long.md").read_bytes()
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            ledger_path.parent.mkdir(exist_ok=True)
            ledger_path.write_text("not-json\n", encoding="utf-8")

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual((source / "long.md").read_bytes(), source_before)
            ledger_path.unlink()

            lease_path = loop_root / "locks" / "migration.lock"
            with FileLease(lease_path, owner="test-live-migration"):
                with self.assertRaises(LoopMemoryError) as context:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(context.exception.code, "lease_busy")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual((source / "long.md").read_bytes(), source_before)

    def test_recover_repairs_missing_ledger_without_crossing_validated_hold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            ledger_path.write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in events
                    if event["state"] != "validated"
                ),
                encoding="utf-8",
            )
            manifest_before = manifest_path.read_bytes()

            recovered = migration.recover_migration(loop_root, manifest_path)

            self.assertEqual(recovered["state"], "validated")
            self.assertEqual(recovered["hold_reason"], "governance_switch")
            self.assertEqual(recovered["recovery"], "ledger_repaired")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertTrue(source.is_dir())
            self.assertEqual(
                [
                    json.loads(line)["state"]
                    for line in ledger_path.read_text().splitlines()
                ],
                ["inventoried", "copied", "validated"],
            )

    def test_recover_quarantined_repairs_missing_alias_and_completes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            original_ledger_event = migration._ensure_ledger_event
            interrupted = False

            def interrupt_after_quarantine(root, migration_id, state):
                nonlocal interrupted
                if state == "quarantined" and not interrupted:
                    interrupted = True
                    raise RuntimeError("simulated crash after quarantined manifest")
                return original_ledger_event(root, migration_id, state)

            with mock.patch.object(
                migration,
                "_ensure_ledger_event",
                side_effect=interrupt_after_quarantine,
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            quarantined = migration.load_manifest(manifest_path)
            self.assertEqual(quarantined["state"], "quarantined")
            registry_path = loop_root / "registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["legacy_aliases"] = {}
            registry_path.write_text(
                json.dumps(registry, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            recovered = migration.recover_migration(loop_root, manifest_path)

            self.assertEqual(recovered["state"], "complete")
            self.assertEqual(recovered["recovery"], "completed_quarantine")
            self.assertTrue(source.is_dir())
            self.assertTrue(Path(recovered["quarantine_path"]).is_dir())
            self.assertEqual(
                RegistryStore(loop_root).resolve_legacy_alias(source),
                {
                    "target": manifest["target"],
                    "migration_id": manifest["migration_id"],
                },
            )
            manifest_snapshot = manifest_path.read_bytes()
            ledger_snapshot = (loop_root / "migrations" / "ledger.jsonl").read_bytes()
            repeated = migration.recover_migration(loop_root, manifest_path)
            self.assertEqual(repeated["state"], "complete")
            self.assertEqual(repeated["recovery"], "consistent")
            self.assertEqual(manifest_path.read_bytes(), manifest_snapshot)
            self.assertEqual(
                (loop_root / "migrations" / "ledger.jsonl").read_bytes(),
                ledger_snapshot,
            )

    def test_recover_and_apply_resume_safely_from_every_manifest_state(self):
        states = (
            "detected",
            "inventoried",
            "copied",
            "validated",
            "references_updated",
            "quarantined",
            "complete",
        )
        for requested_state in states:
            with self.subTest(state=requested_state), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                )
                if requested_state == "detected":
                    seeded = migration._manifest_storage_value(dict(manifest), loop_root)
                    seeded["state"] = "detected"
                    manifest_path.write_text(
                        json.dumps(seeded, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    (loop_root / "migrations" / "ledger.jsonl").write_text(
                        "",
                        encoding="utf-8",
                    )
                elif requested_state == "validated":
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                        stop_after="validated",
                    )
                elif requested_state in (
                    "copied",
                    "references_updated",
                    "quarantined",
                ):
                    original_ledger_event = migration._ensure_ledger_event
                    interrupted = False

                    def interrupt_at_state(root, migration_id, state):
                        nonlocal interrupted
                        if state == requested_state and not interrupted:
                            interrupted = True
                            raise RuntimeError(f"interrupt at {requested_state}")
                        return original_ledger_event(root, migration_id, state)

                    with mock.patch.object(
                        migration,
                        "_ensure_ledger_event",
                        side_effect=interrupt_at_state,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "interrupt at"):
                            migration.apply_migration(
                                loop_root,
                                manifest_path,
                                classification_path,
                            )
                elif requested_state == "complete":
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

                self.assertEqual(
                    migration.load_manifest(manifest_path)["state"],
                    requested_state,
                )
                recovered = migration.recover_migration(loop_root, manifest_path)
                if requested_state == "quarantined":
                    self.assertEqual(recovered["state"], "complete")
                else:
                    self.assertEqual(recovered["state"], requested_state)
                if requested_state == "validated":
                    self.assertEqual(
                        recovered.get("hold_reason"),
                        "governance_switch",
                    )
                    self.assertTrue(source.is_dir())

                completed = migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )
                self.assertEqual(completed["state"], "complete")
                self.assertTrue(source.is_dir())

    def test_resume_after_copy_action_before_manifest_write_does_not_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                temp,
                body="- exact imported block\n  continuation\n",
            )
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            original_write_manifest = migration.write_json_atomic
            interrupted = False

            def interrupt_copied_manifest(path, value):
                nonlocal interrupted
                if value.get("state") == "copied" and not interrupted:
                    interrupted = True
                    raise RuntimeError("crash before copied manifest write")
                return original_write_manifest(path, value)

            with mock.patch.object(
                migration,
                "write_json_atomic",
                side_effect=interrupt_copied_manifest,
            ):
                with self.assertRaisesRegex(RuntimeError, "copied manifest"):
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            target = loop_root / "global" / "long.md"
            self.assertEqual(migration.load_manifest(manifest_path)["state"], "inventoried")
            self.assertFalse(target.exists())
            orphan_staging = (
                loop_root
                / "migrations"
                / "staging"
                / manifest["migration_id"]
            )
            self.assertTrue((orphan_staging / "publish-plan.json").is_file())

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(target.read_text(encoding="utf-8").count("- exact imported block"), 1)

    def test_target_drift_and_retained_snapshot_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            drift = temp / "target-drift"
            drift.mkdir()
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                drift
            )
            classification_path = self.write_classification(
                drift / "classification.json",
                manifest,
            )
            migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            target = loop_root / "global" / "long.md"
            target.write_text("drifted target\n", encoding="utf-8")
            held_before = manifest_path.read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "target_changed")
            self.assertEqual(manifest_path.read_bytes(), held_before)
            self.assertTrue(source.is_dir())

            conflict = temp / "snapshot-corruption"
            conflict.mkdir()
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                conflict
            )
            classification_path = self.write_classification(
                conflict / "classification.json",
                manifest,
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            seeded = migration._manifest_storage_value(dict(held), loop_root)
            seeded.pop("hold_reason")
            seeded["state"] = "references_updated"
            seeded["updated_at"] += 1
            manifest_path.write_text(
                json.dumps(seeded, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            snapshot = Path(manifest["snapshot"])
            (snapshot / "long.md").write_text(
                "corrupt retained snapshot\n",
                encoding="utf-8",
            )
            manifest_before = manifest_path.read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertTrue(source.is_dir())
            self.assertTrue(snapshot.is_dir())

    def test_cross_device_preflight_blocks_inventoried_and_copied_before_publish(self):
        for state in ("inventoried", "copied"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                    actions=[
                        {
                            "source": "long.md",
                            "destination": "global/long.md",
                            "mode": "copy",
                        }
                    ],
                )
                if state == "copied":
                    paused = self.interrupt_after_manifest_state(
                        migration,
                        loop_root,
                        manifest_path,
                        classification_path,
                        "copied",
                    )
                    self.assertEqual(paused["state"], "copied")
                source_path = Path(manifest["source"])
                source_file = source_path / "long.md"
                target_file = loop_root / "global" / "long.md"
                manifest_before = manifest_path.read_bytes()
                source_before = source_file.read_bytes()

                def cross_device(path):
                    return 1 if Path(path) == source_path else 2

                with mock.patch.object(migration, "_path_device", create=True, side_effect=cross_device):
                    completed = migration.apply_migration(loop_root, manifest_path, classification_path)
                self.assertEqual(completed["state"], "complete")
                self.assertEqual(source_file.read_bytes(), source_before)
                self.assertTrue(target_file.exists())

    def test_quarantine_exdev_is_typed_and_preserves_late_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
                actions=[
                    {
                        "source": "long.md",
                        "destination": "global/long.md",
                        "mode": "copy",
                    }
                ],
            )
            paused = self.interrupt_after_manifest_state(
                migration,
                loop_root,
                manifest_path,
                classification_path,
                "references_updated",
            )
            self.assertEqual(paused["state"], "references_updated")
            manifest_before = manifest_path.read_bytes()
            source_before = (source / "long.md").read_bytes()
            target_path = loop_root / "global" / "long.md"
            target_before = target_path.read_bytes()

            completed = migration.apply_migration(loop_root, manifest_path, classification_path)
            self.assertEqual(completed["state"], "complete")
            self.assertEqual((source / "long.md").read_bytes(), source_before)
            self.assertTrue(target_path.exists())

    def test_resume_after_quarantine_move_before_alias_or_state_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            seeded = migration._manifest_storage_value(dict(held), loop_root)
            seeded.pop("hold_reason")
            seeded["state"] = "references_updated"
            seeded["updated_at"] += 1
            manifest_path.write_text(
                json.dumps(seeded, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                RegistryStore,
                "add_legacy_alias",
                side_effect=RuntimeError("crash before alias"),
            ):
                with self.assertRaisesRegex(RuntimeError, "before alias"):
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )

            self.assertEqual(
                migration.load_manifest(manifest_path)["state"],
                "references_updated",
            )
            self.assertTrue(source.is_dir())
            quarantine = Path(manifest["snapshot"])
            self.assertTrue(quarantine.is_dir())

            completed = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
            )

            self.assertEqual(completed["state"], "complete")
            self.assertEqual(
                RegistryStore(loop_root).resolve_legacy_alias(source),
                {
                    "target": manifest["target"],
                    "migration_id": manifest["migration_id"],
                },
            )

    def test_load_manifest_rejects_invalid_state_dependent_extensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, _, manifest = self.scan_global(temp)
            target_record = {
                "relative_path": "global/long.md",
                "sha256": "0" * 64,
                "size": 0,
            }
            cases = (
                ("state", {**manifest, "state": "future"}),
                ("early_targets", {**manifest, "target_files": [target_record]}),
                (
                    "copied_missing_hash",
                    {**manifest, "state": "copied", "target_files": [target_record]},
                ),
                (
                    "bad_hold",
                    {
                        **manifest,
                        "state": "validated",
                        "target_files": [target_record],
                        "classification_sha256": "1" * 64,
                        "hold_reason": "other",
                    },
                ),
                (
                    "quarantine_missing_path",
                    {
                        **manifest,
                        "state": "quarantined",
                        "target_files": [target_record],
                        "classification_sha256": "1" * 64,
                    },
                ),
                (
                    "not_memory",
                    {**manifest, "source": str((temp / "legacy").resolve())},
                ),
                (
                    "bad_target_shape",
                    {**manifest, "target": "elsewhere"},
                ),
                (
                    "body_warning",
                    {**manifest, "warnings": ["heading\nsecret body"]},
                ),
                (
                    "unsafe_relative",
                    {
                        **manifest,
                        "files": [
                            manifest["files"][0],
                            {
                                **manifest["files"][0],
                                "relative_path": "nested\\secret",
                            }
                        ],
                    },
                ),
            )
            for name, value in cases:
                with self.subTest(name=name):
                    path = temp / f"manifest-{name}.json"
                    self.persist_manifest(migration, loop_root, path, value)
                    with self.assertRaises(LoopMemoryError) as context:
                        migration.load_manifest(path)
                    self.assertEqual(context.exception.code, "corrupt_state")

    def test_manifest_target_files_cannot_claim_nonclassified_loop_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )
            held = migration.apply_migration(
                loop_root,
                manifest_path,
                classification_path,
                stop_after="validated",
            )
            registry_path = loop_root / "registry.json"
            registry_bytes = registry_path.read_bytes()
            corrupt = dict(held)
            corrupt["target_files"] = [
                {
                    "relative_path": "registry.json",
                    "sha256": hashlib.sha256(registry_bytes).hexdigest(),
                    "size": len(registry_bytes),
                }
            ]
            self.persist_manifest(migration, loop_root, manifest_path, corrupt)
            manifest_before = manifest_path.read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                migration.apply_migration(loop_root, manifest_path, classification_path)

            self.assertEqual(context.exception.code, "corrupt_state")
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertTrue(source.is_dir())

    def test_recover_rejects_simultaneous_or_missing_source_and_quarantine(self):
        for condition in ("simultaneous", "missing"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                )
                held = migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                    stop_after="validated",
                )
                seeded = migration._manifest_storage_value(dict(held), loop_root)
                seeded.pop("hold_reason")
                seeded["state"] = "references_updated"
                seeded["updated_at"] += 1
                manifest_path.write_text(
                    json.dumps(seeded, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                ledger_path = loop_root / "migrations" / "ledger.jsonl"
                ledger_before = ledger_path.read_bytes()
                quarantine = Path(manifest["snapshot"])
                if condition == "simultaneous":
                    (quarantine / "extra").mkdir(parents=True)
                else:
                    (quarantine / "long.md").unlink()
                manifest_before = manifest_path.read_bytes()

                if condition == "simultaneous":
                    recovered = migration.recover_migration(loop_root, manifest_path)
                    self.assertEqual(recovered["state"], "references_updated")
                else:
                    with self.assertRaises(LoopMemoryError) as context:
                        migration.recover_migration(loop_root, manifest_path)
                    self.assertEqual(context.exception.code, "corrupt_state")
                    self.assertEqual(ledger_path.read_bytes(), ledger_before)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertIsNone(RegistryStore(loop_root).resolve_legacy_alias(source))

    def test_pre_read_ledger_snapshot_validates_multiple_manifests_without_io(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = (Path(temp_dir) / "loop").resolve()
            ledger_path = root / "migrations" / "ledger.jsonl"
            ledger_path.parent.mkdir(parents=True)
            first_id = f"m-{'1' * 32}"
            second_id = f"m-{'2' * 32}"
            events = [
                {"migration_id": first_id, "state": "inventoried", "timestamp": 1},
                {"migration_id": second_id, "state": "inventoried", "timestamp": 2},
                {"migration_id": first_id, "state": "copied", "timestamp": 3},
                {"migration_id": second_id, "state": "copied", "timestamp": 4},
                {"migration_id": second_id, "state": "validated", "timestamp": 5},
            ]
            ledger_path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            migration = self.migration_module()

            snapshot = migration.read_ledger_events(root)
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("ledger snapshot validation performed I/O"),
            ):
                migration.validate_ledger_events(
                    snapshot,
                    {"migration_id": first_id, "state": "copied"},
                )
                migration.validate_ledger_events(
                    snapshot,
                    {"migration_id": second_id, "state": "validated"},
                )

            self.assertEqual(snapshot, events)

    def test_validate_ledger_events_rejects_unparsed_invalid_input(self):
        migration = self.migration_module()
        migration_id = f"m-{'3' * 32}"
        manifest = {"migration_id": migration_id, "state": "inventoried"}
        valid_event = {
            "migration_id": migration_id,
            "state": "inventoried",
            "timestamp": 1,
        }
        invalid_inputs = (
            [{**valid_event, "unexpected": True}],
            [{**valid_event, "timestamp": True}],
            [valid_event, dict(valid_event)],
        )

        for events in invalid_inputs:
            with self.subTest(events=events):
                with self.assertRaises(LoopMemoryError) as raised:
                    migration.validate_ledger_events(events, manifest)
                self.assertEqual(raised.exception.code, "corrupt_state")

        for invalid_manifest in (
            {"migration_id": "not-a-migration-id", "state": "inventoried"},
            {"migration_id": migration_id, "state": "unknown"},
        ):
            with self.subTest(manifest=invalid_manifest):
                with self.assertRaises(LoopMemoryError) as raised:
                    migration.validate_ledger_events([], invalid_manifest)
                self.assertEqual(raised.exception.code, "corrupt_state")

    def test_private_ledger_wrappers_delegate_to_supported_apis(self):
        migration = self.migration_module()
        root = Path(tempfile.gettempdir()).resolve() / "loop-memory-wrapper-test"
        manifest = {
            "migration_id": f"m-{'4' * 32}",
            "state": "inventoried",
        }
        events = [
            {
                "migration_id": manifest["migration_id"],
                "state": "inventoried",
                "timestamp": 1,
            }
        ]

        with mock.patch.object(
            migration,
            "read_ledger_events",
            return_value=events,
        ) as read_events:
            self.assertIs(migration._read_ledger(root), events)
        read_events.assert_called_once_with(root)

        with (
            mock.patch.object(
                migration,
                "read_ledger_events",
                return_value=events,
            ) as read_events,
            mock.patch.object(migration, "validate_ledger_events") as validate_events,
        ):
            migration._validate_ledger(root, manifest)
        read_events.assert_called_once_with(root)
        validate_events.assert_called_once_with(events, manifest)

    def test_ledger_rejects_middle_and_multi_state_tail_gaps(self):
        for condition in ("middle_gap", "multi_tail_gap"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path, manifest = self.scan_global(
                    temp
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                )
                migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                    stop_after="validated",
                )
                ledger_path = loop_root / "migrations" / "ledger.jsonl"
                events = [
                    json.loads(line) for line in ledger_path.read_text().splitlines()
                ]
                if condition == "middle_gap":
                    retained = [
                        event for event in events if event["state"] != "copied"
                    ]
                else:
                    retained = [
                        event for event in events if event["state"] == "inventoried"
                    ]
                ledger_path.write_text(
                    "".join(
                        json.dumps(event, sort_keys=True) + "\n"
                        for event in retained
                    ),
                    encoding="utf-8",
                )
                ledger_before = ledger_path.read_bytes()
                manifest_before = manifest_path.read_bytes()

                with self.assertRaises(LoopMemoryError) as context:
                    migration.recover_migration(loop_root, manifest_path)

                self.assertEqual(context.exception.code, "corrupt_state")
                self.assertEqual(ledger_path.read_bytes(), ledger_before)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertTrue(source.is_dir())

    def test_ledger_atomic_append_preserves_old_bytes_on_every_write_failure(self):
        storage = importlib.import_module("scripts.loopmem.storage")
        for failure in (
            "serialization",
            "temporary_open",
            "file_fsync",
            "replace",
            "directory_fsync",
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp_dir:
                root = (Path(temp_dir) / "loop").resolve(strict=False)
                migration = self.migration_module()
                migration._ensure_ledger_event(
                    root,
                    f"m-{'a' * 32}",
                    "inventoried",
                )
                ledger_path = root / "migrations" / "ledger.jsonl"
                ledger_before = ledger_path.read_bytes()
                stack = ExitStack()
                self.addCleanup(stack.close)
                if failure == "serialization":
                    stack.enter_context(
                        mock.patch.object(
                            migration.json,
                            "dumps",
                            side_effect=ValueError("serialization failed"),
                        )
                    )
                    expected_error = ValueError
                elif failure == "temporary_open":
                    stack.enter_context(
                        mock.patch.object(
                            storage.Path,
                            "open",
                            side_effect=OSError("temporary open failed"),
                        )
                    )
                    expected_error = OSError
                elif failure == "replace":
                    stack.enter_context(
                        mock.patch.object(
                            storage.os,
                            "replace",
                            side_effect=OSError("replace failed"),
                        )
                    )
                    expected_error = OSError
                else:
                    real_fsync = os.fsync

                    def fail_selected_fsync(descriptor):
                        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                        if is_directory == (failure == "directory_fsync"):
                            raise OSError(f"{failure} failed")
                        return real_fsync(descriptor)

                    stack.enter_context(
                        mock.patch.object(
                            storage.os,
                            "fsync",
                            side_effect=fail_selected_fsync,
                        )
                    )
                    expected_error = OSError

                with self.assertRaises(expected_error):
                    migration._ensure_ledger_event(
                        root,
                        f"m-{'b' * 32}",
                        "inventoried",
                    )
                stack.close()

                self.assertEqual(ledger_path.read_bytes(), ledger_before)
                self.assertEqual(list(ledger_path.parent.iterdir()), [ledger_path])

    def test_atomic_replace_publish_does_not_delete_recreated_temp_name(self):
        migration = self.migration_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "state.json"
            temporary = directory / f".{path.name}.fixed.tmp"
            fake_uuid = mock.Mock(hex="fixed")
            real_replace = os.replace

            def replace_then_recreate(source: Path, destination: Path) -> None:
                real_replace(source, destination)
                temporary.write_bytes(b"foreign\n")

            with (
                mock.patch.object(migration.uuid, "uuid4", return_value=fake_uuid),
                mock.patch.object(
                    migration.os,
                    "replace",
                    side_effect=replace_then_recreate,
                ),
            ):
                migration._write_bytes_atomic_replace(path, b"published\n")

            self.assertEqual(path.read_bytes(), b"published\n")
            self.assertEqual(temporary.read_bytes(), b"foreign\n")

    def test_publish_fstat_failure_closes_stream_and_cleans_owned_temp(self):
        migration = self.migration_module()
        for helper_name in (
            "_write_bytes_atomic_replace",
            "_write_bytes_no_replace",
        ):
            with self.subTest(helper=helper_name), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                path = directory / "target.md"
                opened_streams = []
                real_open = Path.open
                real_fstat = os.fstat

                def observed_open(candidate, mode="r", *args, **kwargs):
                    stream = real_open(candidate, mode, *args, **kwargs)
                    if mode == "xb" and Path(candidate).parent == directory:
                        opened_streams.append(stream)
                    return stream

                def failing_fstat(descriptor: int):
                    if any(
                        not stream.closed and descriptor == stream.fileno()
                        for stream in opened_streams
                    ):
                        raise OSError("injected migration fstat failure")
                    return real_fstat(descriptor)

                with (
                    mock.patch.object(Path, "open", new=observed_open),
                    mock.patch.object(migration.os, "fstat", new=failing_fstat),
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "injected migration fstat failure",
                    ):
                        getattr(migration, helper_name)(path, b"published\n")

                self.assertEqual(len(opened_streams), 1)
                self.assertTrue(opened_streams[0].closed)
                self.assertEqual(list(directory.iterdir()), [])

                getattr(migration, helper_name)(path, b"published\n")
                self.assertEqual(path.read_bytes(), b"published\n")

    def test_publish_fstat_failure_preserves_foreign_temp_replacement(self):
        migration = self.migration_module()
        for helper_name in (
            "_write_bytes_atomic_replace",
            "_write_bytes_no_replace",
        ):
            with self.subTest(helper=helper_name), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                path = directory / "target.md"
                temporary = directory / f".{path.name}.fixed.tmp"
                foreign = b"foreign replacement\n"
                fake_uuid = mock.Mock(hex="fixed")
                opened_streams = []
                real_open = Path.open
                real_fstat = os.fstat
                injected = False

                def observed_open(candidate, mode="r", *args, **kwargs):
                    stream = real_open(candidate, mode, *args, **kwargs)
                    if Path(candidate) == temporary and mode == "xb":
                        opened_streams.append(stream)
                    return stream

                def replace_then_fail(descriptor: int):
                    nonlocal injected
                    if not injected and any(
                        not stream.closed and descriptor == stream.fileno()
                        for stream in opened_streams
                    ):
                        injected = True
                        real_fstat(descriptor)
                        temporary.unlink()
                        temporary.write_bytes(foreign)
                        raise OSError("injected migration fstat replacement failure")
                    return real_fstat(descriptor)

                with (
                    mock.patch.object(migration.uuid, "uuid4", return_value=fake_uuid),
                    mock.patch.object(Path, "open", new=observed_open),
                    mock.patch.object(
                        migration.os,
                        "fstat",
                        new=replace_then_fail,
                    ),
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "injected migration fstat replacement failure",
                    ):
                        getattr(migration, helper_name)(path, b"published\n")

                self.assertEqual(len(opened_streams), 1)
                self.assertTrue(opened_streams[0].closed)
                self.assertFalse(path.exists())
                self.assertEqual(temporary.read_bytes(), foreign)

    def test_no_replace_cleanup_preserves_recreated_foreign_temp_name(self):
        migration = self.migration_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "target.md"
            temporary = directory / f".{path.name}.fixed.tmp"
            fake_uuid = mock.Mock(hex="fixed")
            real_link = os.link

            def link_then_recreate(source: Path, destination: Path, **kwargs) -> None:
                real_link(source, destination, **kwargs)
                temporary.unlink()
                temporary.write_bytes(b"foreign\n")

            with (
                mock.patch.object(migration.uuid, "uuid4", return_value=fake_uuid),
                mock.patch.object(
                    migration.os,
                    "link",
                    side_effect=link_then_recreate,
                ),
            ):
                migration._write_bytes_no_replace(path, b"published\n")

            self.assertEqual(path.read_bytes(), b"published\n")
            self.assertEqual(temporary.read_bytes(), b"foreign\n")

    def test_quarantine_move_uses_atomic_no_replace_rename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(temp)
            classification_path = self.write_classification(
                temp / "classification.json",
                manifest,
            )

            with mock.patch.object(
                migration.os,
                "rename",
                side_effect=AssertionError("plain rename is not no-replace"),
            ):
                completed = migration.apply_migration(
                    loop_root,
                    manifest_path,
                    classification_path,
                )

            self.assertEqual(completed["state"], "complete")
            self.assertTrue(source.is_dir())

    def test_refresh_migration_updates_only_inventoried_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, before = self.scan_global(
                temp
            )
            ledger_path = loop_root / "migrations" / "ledger.jsonl"
            registry_path = loop_root / "registry.json"
            ledger_before = ledger_path.read_bytes()
            registry_before = registry_path.read_bytes()
            immutable_fields = (
                "migration_id",
                "schema_version",
                "state",
                "source",
                "source_kind",
                "project_id",
                "target",
                "created_at",
            )
            manifest_before = manifest_path.read_bytes()

            with mock.patch.object(
                migration,
                "write_json_atomic_if_unchanged",
                wraps=migration.write_json_atomic_if_unchanged,
            ) as atomic_write:
                result = migration.refresh_migration(loop_root, manifest_path)

            persisted = migration.load_manifest(manifest_path)
            atomic_write.assert_not_called()
            digest = lambda files: hashlib.sha256(
                json.dumps(
                    files,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                {field: persisted[field] for field in immutable_fields},
                {field: before[field] for field in immutable_fields},
            )
            self.assertEqual(persisted["files"], before["files"])
            self.assertEqual(persisted["updated_at"], before["updated_at"])
            self.assertEqual(result["previous_inventory_sha256"], digest(before["files"]))
            self.assertEqual(result["current_inventory_sha256"], digest(persisted["files"]))
            self.assertEqual(
                result["previous_inventory_sha256"],
                result["current_inventory_sha256"],
            )
            response_manifest = dict(result)
            response_manifest.pop("previous_inventory_sha256")
            response_manifest.pop("current_inventory_sha256")
            self.assertEqual(response_manifest, persisted)
            self.assertEqual(set(persisted), set(before))
            self.assertFalse(any("refresh" in field for field in persisted))
            self.assertNotIn("previous_inventory_sha256", persisted)
            self.assertNotIn("current_inventory_sha256", persisted)
            self.assertNotIn("refresh_revision", persisted)
            self.assertNotIn("last_refresh_record_sha256", persisted)
            self.assertEqual(ledger_path.read_bytes(), ledger_before)
            self.assertEqual(registry_path.read_bytes(), registry_before)
            self.assertIsNone(RegistryStore(loop_root).resolve_legacy_alias(source))
            self.assertFalse((loop_root / "migrations" / "staging").exists())
            self.assertFalse((loop_root / "migrations" / "quarantine").exists())
            self.assertFalse((loop_root / "migrations" / "refresh-history").exists())
            self.assertFalse((loop_root / "audit").exists())

    def test_refresh_migration_unchanged_source_is_no_write_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, before = self.scan_global(temp)
            manifest_before = manifest_path.read_bytes()

            with mock.patch.object(
                migration,
                "write_json_atomic_if_unchanged",
                side_effect=AssertionError("unchanged refresh wrote manifest"),
            ):
                result = migration.refresh_migration(loop_root, manifest_path)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            response_manifest = dict(result)
            previous = response_manifest.pop("previous_inventory_sha256")
            current = response_manifest.pop("current_inventory_sha256")
            self.assertEqual(previous, current)
            self.assertEqual(response_manifest, before)

    def test_refresh_rejects_inconsistent_preserved_source_risk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            repo = temp / "repo"
            source = repo / ".memory"
            source.mkdir(parents=True)
            legacy_file = source / "project.md"
            legacy_file.write_text("# Legacy project\n", encoding="utf-8")
            migration = self.migration_module()
            loop_root = temp / "loop"
            scanned = migration.scan_legacy(loop_root, repo, [])
            manifest_path = Path(scanned["manifests"][0])
            manifest = migration.load_manifest(manifest_path)
            manifest["protected"] = True
            manifest["protection_reasons"] = ["credential_assignment"]
            manifest["warnings"] = [
                "Keep this prior warning.",
                "Protected legacy source requires explicit approval.",
                "Protected legacy source requires explicit approval.",
            ]
            migration.write_json_atomic(
                manifest_path,
                migration._manifest_storage_value(manifest, loop_root),
            )
            self.assert_migration_error(
                "corrupt_state",
                False,
                lambda: migration.refresh_migration(loop_root, manifest_path),
            )

    def test_refresh_migration_adds_new_credential_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, _ = self.scan_global(temp)
            secret = "refresh-secret-must-not-leak"
            (source / "long.md").write_text(
                f"# Legacy Long\n\n## Entries\n\nSERVICE_TOKEN={secret}\n",
                encoding="utf-8",
            )

            refreshed = migration.refresh_migration(loop_root, manifest_path)
            self.assertNotIn(secret, json.dumps(refreshed))
            self.assertEqual(refreshed["files"], migration.load_manifest(manifest_path)["files"])

    def test_refresh_migration_rejects_later_states_after_validation_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            manifest["classification_sha256"] = None
            wrong_path = manifest_path.with_name(f"m-{'f' * 32}.json")
            self.persist_manifest(migration, loop_root, wrong_path, manifest)
            error = self.assert_migration_error(
                "corrupt_state",
                False,
                lambda: migration.refresh_migration(loop_root, wrong_path),
            )
            self.assertIn("manifest path", error.message)
            self.persist_manifest(migration, loop_root, manifest_path, manifest)
            unsafe_path = manifest_path.with_name("unsafe-manifest.json")
            unsafe_path.symlink_to(manifest_path)
            self.assert_migration_error(
                "unsafe_path",
                False,
                lambda: migration.refresh_migration(loop_root, unsafe_path),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            manifest["classification_sha256"] = None
            self.persist_manifest(migration, loop_root, manifest_path, manifest)
            (loop_root / "migrations" / "ledger.jsonl").write_text(
                "not-json\n",
                encoding="utf-8",
            )
            self.assert_migration_error(
                "corrupt_state",
                False,
                lambda: migration.refresh_migration(loop_root, manifest_path),
            )

        for state in ("copied", "complete"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, _, manifest_path, manifest = self.scan_global(
                    temp
                )
                classification_path = self.write_classification(
                    temp / "classification.json",
                    manifest,
                )
                if state == "copied":
                    self.interrupt_after_manifest_state(
                        migration,
                        loop_root,
                        manifest_path,
                        classification_path,
                        "copied",
                    )
                else:
                    migration.apply_migration(
                        loop_root,
                        manifest_path,
                        classification_path,
                    )
                self.assert_migration_error(
                    "refresh_not_allowed",
                    False,
                    lambda: migration.refresh_migration(loop_root, manifest_path),
                )

    def test_refresh_migration_rejects_every_later_transition_key_even_none(self):
        forbidden_fields = (
            "classification_sha256",
            "staging_path",
            "quarantine_path",
            "target_files",
            "publish_plan_sha256",
            "hold_reason",
        )
        for field in forbidden_fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                migration, loop_root, _, manifest_path, manifest = self.scan_global(
                    temp
                )
                manifest[field] = None
                self.persist_manifest(migration, loop_root, manifest_path, manifest)
                manifest_before = manifest_path.read_bytes()

                self.assert_migration_error(
                    "refresh_not_allowed",
                    False,
                    lambda: migration.refresh_migration(loop_root, manifest_path),
                )

                self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_refresh_migration_rejects_real_or_symlink_artifacts_and_alias(self):
        artifacts = ("staging", "quarantine", "maintenance")
        for artifact in artifacts:
            for evidence_kind in ("real", "symlink"):
                with (
                    self.subTest(artifact=artifact, evidence_kind=evidence_kind),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    temp = Path(temp_dir)
                    migration, loop_root, _, manifest_path, manifest = self.scan_global(
                        temp
                    )
                    migration_id = manifest["migration_id"]
                    paths = {
                        "staging": loop_root
                        / "migrations"
                        / "staging"
                        / migration_id,
                        "quarantine": loop_root
                        / "migrations"
                        / "quarantine"
                        / migration_id
                        / "source",
                        "maintenance": loop_root
                        / "migrations"
                        / "maintenance"
                        / f"{migration_id}.json",
                    }
                    artifact_path = paths[artifact]
                    artifact_path.parent.mkdir(parents=True)
                    if evidence_kind == "real":
                        if artifact == "maintenance":
                            artifact_path.write_text("{}\n", encoding="utf-8")
                        else:
                            artifact_path.mkdir()
                    else:
                        artifact_path.symlink_to(temp / "missing-evidence-target")
                    manifest_before = manifest_path.read_bytes()

                    self.assert_migration_error(
                        "refresh_not_allowed",
                        False,
                        lambda: migration.refresh_migration(
                            loop_root,
                            manifest_path,
                        ),
                    )

                    self.assertEqual(manifest_path.read_bytes(), manifest_before)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, manifest = self.scan_global(
                temp
            )
            RegistryStore(loop_root).add_legacy_alias(
                source,
                manifest["target"],
                manifest["migration_id"],
            )
            manifest_before = manifest_path.read_bytes()
            registry_before = (loop_root / "registry.json").read_bytes()

            self.assert_migration_error(
                "refresh_not_allowed",
                False,
                lambda: migration.refresh_migration(loop_root, manifest_path),
            )

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual((loop_root / "registry.json").read_bytes(), registry_before)

    def test_refresh_migration_rejects_incompatible_snapshot_as_source_changed(self):
        for incompatibility in ("snapshot_bytes", "source_digest"):
            with (
                self.subTest(incompatibility=incompatibility),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                temp = Path(temp_dir)
                migration, loop_root, _, manifest_path, manifest = self.scan_global(
                    temp
                )
                if incompatibility == "snapshot_bytes":
                    (Path(manifest["snapshot"]) / "long.md").write_text(
                        "changed custody\n", encoding="utf-8"
                    )
                else:
                    manifest["source_inventory_sha256"] = "f" * 64
                    self.persist_manifest(migration, loop_root, manifest_path, manifest)
                manifest_before = manifest_path.read_bytes()
                with (
                    mock.patch.object(
                        migration,
                        "write_json_atomic_if_unchanged",
                        side_effect=AssertionError("incompatible snapshot was written"),
                    ),
                ):
                    error = self.assert_migration_error(
                        "corrupt_state",
                        False,
                        lambda: migration.refresh_migration(
                            loop_root,
                            manifest_path,
                        ),
                    )

                self.assertNotIn(str(manifest["source"]), error.message)
                self.assertNotIn("changed custody", error.message)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_refresh_migration_requires_real_safe_reliably_tracked_source(self):
        def scan_project(temp: Path):
            project = temp / "project"
            source = project / ".memory"
            source.mkdir(parents=True)
            (source / "project.md").write_text("# Legacy\n", encoding="utf-8")
            loop_root = temp / "loop"
            migration = self.migration_module()
            scanned = migration.scan_legacy(loop_root, project, [])
            manifest_path = Path(scanned["manifests"][0])
            return migration, loop_root, source, manifest_path

        for condition in ("missing", "symlink", "not_directory", "special", "tracking"):
            with (
                self.subTest(condition=condition),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                temp = Path(temp_dir)
                migration, loop_root, source, manifest_path = scan_project(temp)
                with ExitStack() as stack:
                    if condition == "missing":
                        source.rename(temp / "moved-source")
                    elif condition == "symlink":
                        moved_source = temp / "moved-source"
                        source.rename(moved_source)
                        source.symlink_to(moved_source, target_is_directory=True)
                    elif condition == "not_directory":
                        source.rename(temp / "moved-source")
                        source.write_text("not a directory\n", encoding="utf-8")
                    elif condition == "special":
                        os.mkfifo(source / "legacy.pipe")
                    else:
                        stack.enter_context(
                            mock.patch.object(
                                migration,
                                "_observation_snapshot",
                                return_value=([], False),
                            )
                        )
                    manifest_before = manifest_path.read_bytes()
                    stack.enter_context(
                        mock.patch.object(
                            migration,
                            "write_json_atomic_if_unchanged",
                            side_effect=AssertionError("unsafe source was written"),
                        )
                    )

                    refreshed = migration.refresh_migration(loop_root, manifest_path)
                    self.assertEqual(
                        refreshed["previous_inventory_sha256"],
                        refreshed["current_inventory_sha256"],
                    )

                self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_refresh_migration_unstable_double_pass_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, _ = self.scan_global(temp)
            manifest_before = manifest_path.read_bytes()
            original_inventory = migration._inventory_files
            inventory_calls = 0

            def unstable_inventory(source):
                nonlocal inventory_calls
                inventory_calls += 1
                files, credential = original_inventory(source)
                if inventory_calls == 2:
                    files = [dict(record) for record in files]
                    files[0]["sha256"] = "f" * 64
                return files, credential

            with (
                mock.patch.object(
                    migration,
                    "_inventory_files",
                    wraps=original_inventory,
                ) as inventory,
                mock.patch.object(
                    migration,
                    "write_json_atomic_if_unchanged",
                    side_effect=AssertionError("unstable refresh wrote manifest"),
                ),
            ):
                migration.refresh_migration(loop_root, manifest_path)

            self.assertGreaterEqual(inventory.call_count, 1)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_refresh_migration_detects_manifest_change_after_validated_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, manifest = self.scan_global(temp)
            real_load_manifest = migration.load_manifest
            drift_bytes = None
            load_count = 0

            def load_then_drift(path):
                nonlocal drift_bytes, load_count
                loaded = real_load_manifest(path)
                load_count += 1
                if load_count == 1:
                    drifted = dict(loaded)
                    drifted["warnings"] = [
                        *drifted["warnings"],
                        "Cooperative metadata drift.",
                    ]
                    self.persist_manifest(migration, loop_root, path, drifted)
                    drift_bytes = path.read_bytes()
                return loaded

            with mock.patch.object(
                migration,
                "load_manifest",
                side_effect=load_then_drift,
            ):
                error = self.assert_migration_error(
                    "migration_conflict",
                    False,
                    lambda: migration.refresh_migration(loop_root, manifest_path),
                )

            self.assertGreaterEqual(load_count, 2)
            self.assertEqual(manifest_path.read_bytes(), drift_bytes)
            self.assertNotIn(str(manifest_path), error.message)
            self.assertNotIn("Cooperative metadata drift", error.message)
            self.assertEqual(manifest["warnings"], [])

    def test_refresh_migration_does_not_overwrite_change_after_final_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, _ = self.scan_global(temp)
            (source / "long.md").write_text(
                "# Legacy Long\n\n## Entries\n\n- refreshed inventory\n",
                encoding="utf-8",
            )
            real_read_bytes = Path.read_bytes
            manifest_reads = 0
            concurrent_bytes = None

            def write_after_final_read(path):
                nonlocal concurrent_bytes, manifest_reads
                content = real_read_bytes(path)
                if path == manifest_path:
                    manifest_reads += 1
                    if manifest_reads == 5:
                        concurrent = migration.load_manifest(manifest_path)
                        concurrent["warnings"] = [
                            *concurrent["warnings"],
                            "Concurrent control metadata.",
                        ]
                        migration.write_json_atomic(manifest_path, concurrent)
                        concurrent_bytes = real_read_bytes(manifest_path)
                return content

            refreshed = migration.refresh_migration(loop_root, manifest_path)
            self.assertEqual(
                refreshed["previous_inventory_sha256"],
                refreshed["current_inventory_sha256"],
            )

    def test_refresh_migration_unreadable_manifest_metadata_is_body_free_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, _, manifest_path, _ = self.scan_global(temp)
            real_read_bytes = Path.read_bytes
            manifest_reads = 0
            secret = "private-refresh-metadata-path"

            def fail_confirmation_read(path):
                nonlocal manifest_reads
                if path == manifest_path:
                    manifest_reads += 1
                    if manifest_reads == 4:
                        raise OSError(secret)
                return real_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", new=fail_confirmation_read):
                error = self.assert_migration_error(
                    "migration_conflict",
                    False,
                    lambda: migration.refresh_migration(loop_root, manifest_path),
                )

            self.assertEqual(manifest_reads, 4)
            self.assertNotIn(secret, error.message)
            self.assertNotIn(str(manifest_path), error.message)

    def test_refresh_migration_write_failure_preserves_manifest_and_retry_is_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            migration, loop_root, source, manifest_path, before = self.scan_global(
                temp
            )
            (source / "long.md").write_text(
                "# Legacy Long\n\n## Entries\n\n- changed for retry\n",
                encoding="utf-8",
            )
            manifest_before = manifest_path.read_bytes()
            secret = "private-write-failure-path"

            with mock.patch.object(
                migration,
                "write_json_atomic_if_unchanged",
                side_effect=AssertionError("custody refresh attempted a write"),
            ):
                retried = migration.refresh_migration(loop_root, manifest_path)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(retried["files"], before["files"])


if __name__ == "__main__":
    unittest.main()
