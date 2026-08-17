import json
import hashlib
import errno
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.root import (
    RootMetadata,
    convert_v1_metadata,
    publish_conversion,
    validate_conversion_plan,
)


class RootConversionTests(unittest.TestCase):
    def fixture_root(self) -> Path:
        root = Path(__file__).parent / "fixtures" / "schema-v1-root"
        return root

    def copy_fixture(self, destination: Path) -> None:
        shutil.copytree(self.fixture_root(), destination)

    def bind_fixture_root(self, root: Path) -> None:
        def bind(value):
            if isinstance(value, dict):
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            if isinstance(value, str) and value.startswith("@ROOT@/"):
                return str(root / value.removeprefix("@ROOT@/"))
            return value

        for path in root.rglob("*.json"):
            value = json.loads(path.read_text())
            bound = bind(value)
            if bound != value:
                path.write_text(json.dumps(bound, sort_keys=True) + "\n")

    def test_v1_metadata_converts_to_stable_relative_schema_v2(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            self.assertEqual(
                plan.root_metadata,
                RootMetadata(
                    schema_version=2,
                    root_id="r-legacy-root",
                    owner_uid=os.getuid(),
                    generation=1,
                    layout="relative-paths-v2",
                ),
            )
            validate_conversion_plan(root, plan)
            publish_conversion(root, plan)
            self.assertEqual(json.loads((root / "root.json").read_text()), {
                "schema_version": 2,
                "root_id": "r-legacy-root",
                "owner_uid": os.getuid(),
                "generation": 1,
                "layout": "relative-paths-v2",
            })

    def test_conversion_is_idempotent_and_preserves_markdown_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            body = b"# opaque\n\xff\n"
            (root / "global.md").write_bytes(body)
            first = convert_v1_metadata(root)
            publish_conversion(root, first)
            before = (root / "global.md").read_bytes()
            second = convert_v1_metadata(root)
            self.assertTrue(second.noop)
            publish_conversion(root, second)
            self.assertEqual((root / "global.md").read_bytes(), before)

    def test_unsafe_escape_unknown_duplicate_and_partial_fail_closed(self):
        cases = (
            ("escape", {"target": "../outside"}),
            ("unknown", {"unexpected": "value"}),
            ("duplicate", {"duplicate_id": True}),
            ("partial", {"partial": True}),
        )
        for name, update in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / name
                self.copy_fixture(root)
                self.bind_fixture_root(root)
                if name == "escape":
                    manifest_path = root / "migrations/manifests/m-00000000000000000000000000000000.json"
                    state = json.loads(manifest_path.read_text())
                    state["target"] = update["target"]
                    manifest_path.write_text(json.dumps(state))
                elif name == "unknown":
                    state = json.loads((root / "registry.json").read_text())
                    state.update(update)
                    (root / "registry.json").write_text(json.dumps(state))
                elif name == "duplicate":
                    state = json.loads((root / "registry.json").read_text())
                    state["projects"]["p-one"]["roots"].append(
                        state["projects"]["p-one"]["roots"][0]
                    )
                    (root / "registry.json").write_text(json.dumps(state))
                else:
                    (root / "root.transaction.json").write_text(
                        json.dumps({"phase": "replacing", "metadata": ["root.json"]})
                    )
                with self.assertRaises(LoopMemoryError):
                    convert_v1_metadata(root)

    def test_plan_pins_source_bytes_identity_and_converted_digests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            registry = next(item for item in plan.files if item.relative_path == "registry.json")
            self.assertEqual(registry.source_bytes, (root / "registry.json").read_bytes())
            self.assertEqual(registry.source_identity, self.identity(root / "registry.json"))
            self.assertEqual(len(registry.converted_sha256), 64)
            (root / "registry.json").write_text("{}\n")
            with self.assertRaises(LoopMemoryError) as caught:
                validate_conversion_plan(root, plan)
            self.assertEqual(caught.exception.code, "conversion_conflict")

    def test_registry_and_migration_fields_convert_explicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            publish_conversion(root, plan)
            registry = json.loads((root / "registry.json").read_text())
            self.assertEqual(registry["schema_version"], 2)
            self.assertEqual(registry["generation"], 1)
            self.assertEqual(
                registry["projects"]["p-one"],
                {"roots": ["/external/project", "/external/project-alias"]},
            )
            self.assertEqual(registry["sessions"]["s-one"], {
                "project_id": "p-one",
                "platform_session_id": "host-id",
                "generation": 1,
                "resumes_from": None,
                "state": "active",
            })
            self.assertEqual(
                registry["legacy_aliases"]["/external/legacy/.memory"]["target"],
                "projects/p-one",
            )
            manifest = json.loads(
                (root / "migrations/manifests/m-00000000000000000000000000000000.json").read_text()
            )
            self.assertEqual(manifest["source"], "/external/legacy/.memory")
            self.assertEqual(manifest["target"], "projects/p-one")
            self.assertNotIn("staging_path", manifest)
            self.assertNotIn("quarantine_path", manifest)

    def test_published_conversion_is_accepted_by_all_v2_consumers(self):
        from scripts.loopmem import migration
        from scripts.loopmem import maintenance
        from scripts.loopmem.registry import RegistryStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            manifest_path = root / "migrations/manifests/m-00000000000000000000000000000000.json"
            plan = convert_v1_metadata(root)
            publish_conversion(root, plan)
            loaded = migration.load_manifest(manifest_path)
            self.assertEqual(
                loaded["target"],
                str((root / "projects/p-one").resolve()),
            )
            RegistryStore(root).validate()
            marker_path = root / "migrations/maintenance/m-00000000000000000000000000000000.json"
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps({
                "schema_version": 2,
                "migration_id": loaded["migration_id"],
                "manifest_sha256": "0" * 64,
                "phase": "complete",
            }))
            marker = maintenance._load_cleanup_marker_snapshot(marker_path)
            self.assertEqual(marker.value["phase"], "complete")

    def test_missing_registry_collections_and_dangling_references_fail_closed(self):
        cases = ("projects", "sessions", "legacy_aliases", "maintenance")
        for field in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "root"
                self.copy_fixture(root)
                self.bind_fixture_root(root)
                state = json.loads((root / "registry.json").read_text())
                del state[field]
                (root / "registry.json").write_text(json.dumps(state))
                with self.assertRaises(LoopMemoryError):
                    convert_v1_metadata(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            state = json.loads((root / "registry.json").read_text())
            state["sessions"]["s-one"]["project_id"] = "p-missing"
            (root / "registry.json").write_text(json.dumps(state))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

    def test_cross_project_normalized_root_alias_collision_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            state = json.loads((root / "registry.json").read_text())
            state["projects"]["p-two"] = {
                "kind": "directory",
                "common_dirs": [],
                "roots": ["/external/./project"],
                "remotes": [],
            }
            (root / "registry.json").write_text(json.dumps(state))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

    def test_legacy_alias_normalization_collision_and_manifest_binding_fail_closed(self):
        for case in ("collision", "target", "migration"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "root"
                self.copy_fixture(root)
                self.bind_fixture_root(root)
                state = json.loads((root / "registry.json").read_text())
                alias = state["legacy_aliases"]["/external/legacy/.memory"]
                if case == "collision":
                    state["legacy_aliases"]["/external/legacy/./.memory"] = dict(alias)
                elif case == "target":
                    alias["target"] = str(root / "projects/p-missing")
                else:
                    alias["migration_id"] = "m-11111111111111111111111111111111"
                (root / "registry.json").write_text(json.dumps(state))
                with self.assertRaises(LoopMemoryError):
                    convert_v1_metadata(root)

    def test_cross_file_project_and_resume_references_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            manifest_path = root / "migrations/manifests/m-00000000000000000000000000000000.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["project_id"] = "p-missing"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            state = json.loads((root / "registry.json").read_text())
            state["schema_version"] = 2
            state["generation"] = 1
            state["projects"]["p-one"] = {"roots": state["projects"]["p-one"]["roots"]}
            state["sessions"]["s-one"] = {
                "project_id": "p-one",
                "platform_session_id": "host-id",
                "generation": 2,
                "resumes_from": "s-missing",
                "state": "active",
            }
            (root / "registry.json").write_text(json.dumps(state))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

    def test_target_files_and_cleanup_marker_paths_convert_and_validate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            manifest_path = root / "migrations/manifests/m-00000000000000000000000000000000.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["target_files"] = [{
                "relative_path": str(root / "projects/p-one/project.md"),
                "sha256": "0" * 64,
                "size": 0,
            }]
            manifest_path.write_text(json.dumps(manifest))
            maintenance = root / "migrations/maintenance"
            maintenance.mkdir(parents=True)
            marker = maintenance / "m-00000000000000000000000000000000.json"
            marker.write_text(json.dumps({
                "schema_version": 1,
                "migration_id": "m-00000000000000000000000000000000",
                "manifest_sha256": "0" * 64,
                "manifest_identity": [1, 2],
                "phase": "quarantine_deleting",
                "quarantine_path": str(root / "migrations/quarantine/m-00000000000000000000000000000000"),
                "quarantine_identity": [3, 4],
                "quarantine_mtime": 1,
                "staging_path": str(root / "migrations/staging/m-00000000000000000000000000000000"),
                "staging_identity": [5, 6],
                "staging_mtime": 1,
            }))
            plan = convert_v1_metadata(root)
            publish_conversion(root, plan)
            converted_manifest = json.loads(manifest_path.read_text())
            self.assertEqual(
                converted_manifest["target_files"][0]["relative_path"],
                "projects/p-one/project.md",
            )
            converted_marker = json.loads(marker.read_text())
            self.assertEqual(
                converted_marker["quarantine_path"],
                "migrations/quarantine/m-00000000000000000000000000000000",
            )
            expected_manifest_digest = __import__("hashlib").sha256(
                json.dumps(
                    converted_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                converted_marker["manifest_sha256"],
                expected_manifest_digest,
            )
            self.assertNotIn("manifest_identity", converted_marker)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            maintenance = root / "migrations/maintenance"
            maintenance.mkdir(parents=True)
            (maintenance / "m-00000000000000000000000000000000.json").write_text(
                json.dumps({"schema_version": 1, "phase": "complete"})
            )
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

    def test_v2_manifest_loader_accepts_relative_internal_paths(self):
        from scripts.loopmem import migration

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            root.mkdir()
            (root / "projects/p-one").mkdir(parents=True)
            manifests = root / "migrations/manifests"
            manifests.mkdir(parents=True)
            manifest = {
                "migration_id": "m-00000000000000000000000000000000",
                "schema_version": 2,
                "state": "inventoried",
                "source": "/external/legacy/.memory",
                "source_kind": "empty",
                "project_id": "p-one",
                "catalogued_files": [],
                "files": [],
                "snapshot": "migrations/quarantine/m-00000000000000000000000000000000/source",
                "source_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
                "target": "projects/p-one",
                "created_at": 1,
                "updated_at": 1,
                "warnings": [],
            }
            path = manifests / f"{manifest['migration_id']}.json"
            path.write_text(json.dumps(manifest))
            loaded = migration.load_manifest(path)
            self.assertEqual(
                loaded["target"],
                str((root / "projects/p-one").resolve()),
            )

    def test_failure_before_every_replace_is_resumable(self):
        for stop_index in range(3):
            with self.subTest(stop_index=stop_index), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "root"
                self.copy_fixture(root)
                self.bind_fixture_root(root)
                plan = convert_v1_metadata(root)
                from scripts.loopmem import root as root_module

                original_replace = root_module.os.replace
                calls = 0

                def fail_at(source, target):
                    nonlocal calls
                    if calls == stop_index:
                        calls += 1
                        raise OSError("injected")
                    calls += 1
                    return original_replace(source, target)

                with mock.patch.object(root_module.os, "replace", side_effect=fail_at):
                    with self.assertRaises(OSError):
                        publish_conversion(root, plan)
                resumed = convert_v1_metadata(root)
                publish_conversion(root, resumed)
                self.assertEqual(json.loads((root / "root.json").read_text())["schema_version"], 2)
                self.assertEqual(json.loads((root / "registry.json").read_text())["schema_version"], 2)
                self.assertFalse((root / "root.transaction.json").exists())

    def test_transaction_rejects_forged_entries_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            transaction = root / "root.transaction.json"
            transaction.write_text(json.dumps({
                "schema_version": 1,
                "root_id": plan.root_metadata.root_id,
                "generation": plan.root_metadata.generation,
                "entries": [{
                    "relative_path": "../victim",
                    "source_sha256": "0" * 64,
                    "source_identity": [1, 2],
                    "converted_sha256": "0" * 64,
                    "converted_bytes": "",
                    "replaced": False,
                }],
            }))
            victim = Path(temp_dir) / "victim"
            with self.assertRaises(LoopMemoryError):
                publish_conversion(root, plan)
            self.assertFalse(victim.exists())

    def test_transaction_rejects_memory_body_even_with_valid_digests(self):
        import base64
        import hashlib

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            body = root / "global.md"
            original = b"# durable body\n"
            body.write_bytes(original)
            plan = convert_v1_metadata(root)
            malicious = b"# replaced body\n"
            (root / "root.transaction.json").write_text(json.dumps({
                "schema_version": 1,
                "root_metadata": plan.root_metadata.as_dict(),
                "entries": [{
                    "relative_path": "global.md",
                    "source_sha256": hashlib.sha256(original).hexdigest(),
                    "source_identity": list(self.identity(body)),
                    "source_bytes": base64.b64encode(original).decode("ascii"),
                    "converted_sha256": hashlib.sha256(malicious).hexdigest(),
                    "converted_bytes": base64.b64encode(malicious).decode("ascii"),
                    "replaced": False,
                }],
            }))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)
            self.assertEqual(body.read_bytes(), original)

    def test_transaction_cannot_replace_registry_with_self_authenticated_payload(self):
        import base64
        import hashlib

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            original = (root / "registry.json").read_bytes()
            plan = convert_v1_metadata(root)
            forged = json.loads(original)
            forged["schema_version"] = 2
            forged["generation"] = 999
            forged["projects"] = {"p-evil": {"roots": ["/external/evil"]}}
            forged["sessions"] = {}
            forged["legacy_aliases"] = {}
            forged_bytes = json.dumps(
                forged, sort_keys=True, separators=(",", ":")
            ).encode() + b"\n"
            transaction = {
                "schema_version": 1,
                "root_metadata": plan.root_metadata.as_dict(),
                "entries": [{
                    "relative_path": "registry.json",
                    "source_sha256": hashlib.sha256(original).hexdigest(),
                    "source_identity": list(self.identity(root / "registry.json")),
                    "source_bytes": base64.b64encode(original).decode(),
                    "converted_sha256": hashlib.sha256(forged_bytes).hexdigest(),
                    "converted_bytes": base64.b64encode(forged_bytes).decode(),
                    "replaced": False,
                }],
            }
            (root / "root.transaction.json").write_text(json.dumps(transaction))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)
            self.assertEqual((root / "registry.json").read_bytes(), original)

    def test_transaction_cannot_add_self_authenticated_manifest(self):
        import base64
        import hashlib

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            relative = "migrations/manifests/m-forged.json"
            payload = b'{"schema_version":2}\n'
            transaction = {
                "schema_version": 1,
                "root_metadata": plan.root_metadata.as_dict(),
                "entries": [{
                    "relative_path": relative,
                    "source_sha256": hashlib.sha256(b"").hexdigest(),
                    "source_identity": [0, 0],
                    "source_bytes": "",
                    "converted_sha256": hashlib.sha256(payload).hexdigest(),
                    "converted_bytes": base64.b64encode(payload).decode(),
                    "replaced": False,
                }],
            }
            (root / "root.transaction.json").write_text(json.dumps(transaction))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)
            self.assertFalse((root / relative).exists())

    def test_self_consistent_replaced_journal_cannot_authorize_forged_converted_registry(self):
        import base64
        import hashlib
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            original_identity = self.identity(root / "registry.json")
            plan = convert_v1_metadata(root)
            original = next(
                item.converted_bytes
                for item in plan.files
                if item.relative_path == "registry.json"
            )
            forged = json.loads(original)
            forged["generation"] = 999
            forged_bytes = json.dumps(
                forged, sort_keys=True, separators=(",", ":")
            ).encode() + b"\n"
            (root / "registry.json").write_bytes(forged_bytes)
            entry = {
                "relative_path": "registry.json",
                "source_sha256": hashlib.sha256(original).hexdigest(),
                "source_identity": list(original_identity),
                "source_bytes": base64.b64encode(original).decode(),
                "converted_sha256": hashlib.sha256(forged_bytes).hexdigest(),
                "converted_bytes": base64.b64encode(forged_bytes).decode(),
                "replaced": True,
            }
            entries = [root_module._transaction_entry(item) for item in plan.files]
            entries = [entry if item["relative_path"] == "registry.json" else item for item in entries]
            entries = [dict(item, replaced=True) for item in entries]
            forged_plan = root_module.ConversionPlan(
                plan.root_metadata,
                tuple(root_module.ConversionFile(
                    relative_path=item["relative_path"],
                    source_bytes=base64.b64decode(item["source_bytes"]),
                    source_identity=tuple(item["source_identity"]),
                    converted_bytes=base64.b64decode(item["converted_bytes"]),
                    converted_sha256=item["converted_sha256"],
                ) for item in entries),
            )
            transaction = root / "root.transaction.json"
            transaction.write_text(json.dumps({
                "schema_version": 1,
                "root_metadata": plan.root_metadata.as_dict(),
                "plan_digest": root_module._plan_digest(forged_plan),
                "transaction_id": "a" * 32,
                "entries": entries,
            }))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)
            self.assertEqual((root / "registry.json").read_bytes(), forged_bytes)
            self.assertTrue(transaction.exists())

    def test_recovery_rejects_journal_missing_registry_entry_without_changes(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            missing_registry = root_module.ConversionPlan(
                plan.root_metadata,
                tuple(
                    item for item in plan.files if item.relative_path != "registry.json"
                ),
            )
            entries = [
                root_module._transaction_entry(item)
                for item in missing_registry.files
            ]
            transaction = root / "root.transaction.json"
            transaction.write_text(json.dumps({
                "schema_version": 1,
                "root_metadata": missing_registry.root_metadata.as_dict(),
                "plan_digest": root_module._plan_digest(missing_registry),
                "transaction_id": "b" * 32,
                "entries": entries,
            }, sort_keys=True) + "\n")
            watched = [
                root / "root.json",
                root / "registry.json",
                root / "migrations/manifests/m-00000000000000000000000000000000.json",
                transaction,
            ]
            before = {path: path.read_bytes() for path in watched}
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)
            self.assertEqual({path: path.read_bytes() for path in watched}, before)

    def test_recovery_rejects_missing_root_file_and_missing_root_plan_entry(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            (root / "root.json").unlink()
            missing_root = root_module.ConversionPlan(
                plan.root_metadata,
                tuple(item for item in plan.files if item.relative_path != "root.json"),
            )
            transaction = root / "root.transaction.json"
            transaction.write_text(json.dumps({
                "schema_version": 1,
                "root_metadata": missing_root.root_metadata.as_dict(),
                "plan_digest": root_module._plan_digest(missing_root),
                "transaction_id": "c" * 32,
                "entries": [
                    root_module._transaction_entry(item)
                    for item in missing_root.files
                ],
            }, sort_keys=True) + "\n")
            watched = [
                root / "registry.json",
                root / "migrations/manifests/m-00000000000000000000000000000000.json",
                transaction,
            ]
            before = {path: path.read_bytes() for path in watched}
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)
            self.assertEqual({path: path.read_bytes() for path in watched}, before)
            self.assertFalse((root / "root.json").exists())

    def test_partial_root_metadata_is_not_completed_implicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            (root / "root.json").write_text(json.dumps({"root_id": "r-fixed"}))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

    def test_recovery_uses_transaction_root_identity_not_random_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            self.copy_fixture(root)
            self.bind_fixture_root(root)
            plan = convert_v1_metadata(root)
            transaction = root / "root.transaction.json"
            transaction.write_text(json.dumps({
                "schema_version": 1,
                "root_id": "r-recovery-fixed",
                "generation": 9,
                "entries": [],
            }))
            with self.assertRaises(LoopMemoryError):
                convert_v1_metadata(root)

    @staticmethod
    def identity(path: Path) -> tuple[int, int]:
        value = path.stat()
        return value.st_dev, value.st_ino


class RootRelocationTests(unittest.TestCase):
    def fixture_root(self) -> Path:
        return Path(__file__).parent / "fixtures" / "schema-v1-root"

    def make_old_root(self, root: Path) -> None:
        shutil.copytree(self.fixture_root(), root)

        def bind(value):
            if isinstance(value, dict):
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            if isinstance(value, str) and value.startswith("@ROOT@/"):
                return str(root / value.removeprefix("@ROOT@/"))
            return value

        for path in root.rglob("*.json"):
            value = json.loads(path.read_text())
            path.write_text(json.dumps(bind(value), sort_keys=True) + "\n")

    def make_new_root(self, root: Path) -> None:
        self.make_old_root(root)
        plan = convert_v1_metadata(root)
        publish_conversion(root, plan)

    def converge(self, old_root: Path, new_root: Path, **kwargs) -> Path:
        from scripts.loopmem.convergence import converge_root_authority

        return converge_root_authority(
            old_root=old_root,
            new_root=new_root,
            **kwargs,
        )

    def test_only_old_root_is_converted_then_atomically_relocated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            body = b"# opaque memory\n\xff\n"
            (old_root / "global.md").write_bytes(body)

            resolved = self.converge(old_root, new_root)

            self.assertEqual(resolved, new_root)
            self.assertFalse(old_root.exists())
            self.assertTrue(new_root.is_dir())
            self.assertFalse(new_root.is_symlink())
            self.assertEqual((new_root / "global.md").read_bytes(), body)
            self.assertEqual(
                json.loads((new_root / "root.json").read_text())["schema_version"],
                2,
            )
            self.assertEqual(
                json.loads((new_root / "registry.json").read_text())["schema_version"],
                2,
            )

    def test_metadata_less_v1_authority_is_transactionally_relocated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            (old_root / "root.json").unlink()
            registry_before = (old_root / "registry.json").read_bytes()

            self.assertEqual(self.converge(old_root, new_root), new_root)

            self.assertFalse(old_root.exists())
            self.assertTrue((new_root / "root.json").is_file())
            self.assertEqual(
                json.loads((new_root / "root.json").read_text())["schema_version"],
                2,
            )
            self.assertNotEqual(
                (new_root / "registry.json").read_bytes(),
                registry_before,
            )
            self.assertEqual(
                json.loads((new_root / "relocation.json").read_text())["phase"],
                "complete",
            )

    def test_metadata_less_original_v1_registry_vocabulary_is_relocated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            (old_root / "root.json").unlink()
            registry = old_root / "registry.json"
            value = json.loads(registry.read_text())
            value["projects"]["p-one"] = {
                "kind": "git",
                "common_dirs": [str(home / "fixture-common.git")],
                "roots": [str(home / "fixture-project")],
                "remotes": ["https://example.test/fixture.git"],
            }
            registry.write_text(json.dumps(value, sort_keys=True) + "\n")

            self.assertEqual(self.converge(old_root, new_root), new_root)

            converted = json.loads((new_root / "registry.json").read_text())
            self.assertEqual(converted["schema_version"], 2)
            self.assertEqual(
                converted["projects"]["p-one"],
                {"roots": [str(home / "fixture-project")]},
            )

    def test_original_v1_registry_vocabulary_rejects_unsafe_shapes(self):
        cases = (
            ("unknown", {"unknown": []}),
            ("bad-kind", {"kind": "repository"}),
            ("missing-common", {"common_dirs": []}),
            ("bad-remote", {"remotes": [" padded "]}),
        )
        for case, update in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir).resolve()
                old_root = home / ".codex" / "loop-memory"
                new_root = home / "loop-memory"
                self.make_old_root(old_root)
                (old_root / "root.json").unlink()
                registry = old_root / "registry.json"
                value = json.loads(registry.read_text())
                record = {
                    "kind": "git",
                    "common_dirs": [str(home / "fixture-common.git")],
                    "roots": [str(home / "fixture-project")],
                    "remotes": ["https://example.test/fixture.git"],
                }
                record.update(update)
                value["projects"]["p-one"] = record
                registry.write_text(json.dumps(value, sort_keys=True) + "\n")

                with self.assertRaises(LoopMemoryError):
                    self.converge(old_root, new_root)

                self.assertTrue(old_root.is_dir())
                self.assertFalse(new_root.exists())

    def test_metadata_less_original_v1_manifest_vocabulary_is_relocated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            (old_root / "root.json").unlink()
            manifest = next((old_root / "migrations/manifests").glob("*.json"))
            value = json.loads(manifest.read_text())
            value["tracked_files"] = value.pop("catalogued_files")
            manifest.write_text(json.dumps(value, sort_keys=True) + "\n")

            self.assertEqual(self.converge(old_root, new_root), new_root)

            converted = json.loads(
                (new_root / "migrations/manifests" / manifest.name).read_text()
            )
            self.assertIn("catalogued_files", converted)
            self.assertNotIn("tracked_files", converted)

    def test_original_v1_manifest_vocabulary_rejects_mixed_tracking_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            (old_root / "root.json").unlink()
            manifest = next((old_root / "migrations/manifests").glob("*.json"))
            value = json.loads(manifest.read_text())
            value["tracked_files"] = value["catalogued_files"]
            manifest.write_text(json.dumps(value, sort_keys=True) + "\n")

            with self.assertRaises(LoopMemoryError):
                self.converge(old_root, new_root)

            self.assertTrue(old_root.is_dir())
            self.assertFalse(new_root.exists())

    def test_metadata_less_authority_still_requires_a_regular_v1_registry(self):
        for case in ("missing", "symlink", "version-two"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir).resolve()
                old_root = home / ".codex" / "loop-memory"
                new_root = home / "loop-memory"
                self.make_old_root(old_root)
                (old_root / "root.json").unlink()
                registry = old_root / "registry.json"
                if case == "missing":
                    registry.unlink()
                elif case == "symlink":
                    registry.unlink()
                    registry.symlink_to(old_root / "global" / "long.md")
                else:
                    value = json.loads(registry.read_text())
                    value["schema_version"] = 2
                    registry.write_text(json.dumps(value) + "\n")

                with self.assertRaises(LoopMemoryError):
                    self.converge(old_root, new_root)

                self.assertTrue(old_root.is_dir())
                self.assertFalse(new_root.exists())

    def test_only_new_root_is_returned_without_creating_a_relocation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_new_root(new_root)
            before = {
                path.relative_to(new_root): path.read_bytes()
                for path in new_root.rglob("*")
                if path.is_file()
            }

            self.assertEqual(self.converge(old_root, new_root), new_root)

            after = {
                path.relative_to(new_root): path.read_bytes()
                for path in new_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse((new_root / "relocation.json").exists())

    def test_semantically_stable_v2_json_formatting_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve()
            new_root = home / "loop-memory"
            self.make_new_root(new_root)
            registry = new_root / "registry.json"
            value = json.loads(registry.read_text())
            registry.write_text(json.dumps(value, indent=2) + "\n")

            self.assertEqual(self.converge(home / ".codex" / "loop-memory", new_root), new_root)

    def test_neither_root_exists_selects_new_root_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"

            self.assertEqual(self.converge(old_root, new_root), new_root)
            self.assertFalse(old_root.exists())
            self.assertFalse(new_root.exists())

    def test_two_valid_roots_fail_with_typed_conflict_without_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_new_root(old_root)
            self.make_new_root(new_root)
            watched = (old_root / "root.json", new_root / "root.json")
            before = tuple(path.read_bytes() for path in watched)

            with self.assertRaises(LoopMemoryError) as caught:
                self.converge(old_root, new_root)

            self.assertEqual(caught.exception.code, "root_conflict")
            self.assertEqual(tuple(path.read_bytes() for path in watched), before)

    def test_old_root_or_component_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            real = home / "real"
            self.make_old_root(real)
            linked_root = home / ".codex" / "loop-memory"
            linked_root.parent.mkdir()
            linked_root.symlink_to(real, target_is_directory=True)
            with self.assertRaises(LoopMemoryError) as caught:
                self.converge(linked_root, home / "loop-memory")
            self.assertEqual(caught.exception.code, "unsafe_path")

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            real_parent = home / "real-codex"
            old_root = real_parent / "loop-memory"
            self.make_old_root(old_root)
            linked_parent = home / ".codex"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(LoopMemoryError) as caught:
                self.converge(linked_parent / "loop-memory", home / "loop-memory")
            self.assertEqual(caught.exception.code, "unsafe_path")

    def test_wrong_filesystem_owner_fails_before_transaction_creation(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            with mock.patch.object(
                root_module.os,
                "getuid",
                return_value=os.getuid() + 1,
            ):
                with self.assertRaises(LoopMemoryError) as caught:
                    self.converge(old_root, new_root)
            self.assertEqual(caught.exception.code, "invalid_root_owner")
            self.assertFalse((old_root / "relocation.json").exists())

    def test_active_lease_and_unknown_transaction_block_relocation(self):
        cases = ("active_lease", "unknown_transaction")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir)
                old_root = home / ".codex" / "loop-memory"
                new_root = home / "loop-memory"
                self.make_old_root(old_root)
                if case == "active_lease":
                    locks = old_root / "locks"
                    locks.mkdir()
                    (locks / "worker.lock").write_text(json.dumps({
                        "owner": "worker",
                        "pid": os.getpid(),
                        "acquired_at": 1.0,
                        "expires_at": 99999999999.0,
                        "token": "held",
                    }))
                    expected = "root_relocation_busy"
                else:
                    (old_root / "write.transaction.json").write_text("{}\n")
                    expected = "root_transaction_conflict"

                with self.assertRaises(LoopMemoryError) as caught:
                    self.converge(old_root, new_root)

                self.assertEqual(caught.exception.code, expected)
                self.assertTrue(old_root.is_dir())
                self.assertFalse(new_root.exists())

    def test_exdev_from_atomic_rename_is_typed_and_keeps_old_authority(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            failure = OSError(errno.EXDEV, "cross-device link")

            with mock.patch.object(
                root_module,
                "_native_rename_noreplace",
                side_effect=failure,
            ):
                with self.assertRaises(LoopMemoryError) as caught:
                    self.converge(old_root, new_root)

            self.assertEqual(caught.exception.code, "root_relocation_cross_device")
            self.assertTrue(old_root.is_dir())
            self.assertFalse(new_root.exists())
            self.assertEqual(
                json.loads((old_root / "relocation.json").read_text())["phase"],
                "conversion_published",
            )

    def test_faults_before_publish_and_around_rename_resume_to_one_authority(self):
        for stop_point in (
            "before_conversion_publish",
            "before_root_rename",
            "after_root_rename",
        ):
            with self.subTest(stop_point=stop_point), tempfile.TemporaryDirectory() as temp_dir:
                home = Path(temp_dir)
                old_root = home / ".codex" / "loop-memory"
                new_root = home / "loop-memory"
                self.make_old_root(old_root)

                def fault(point: str) -> None:
                    if point == stop_point:
                        raise RuntimeError("injected relocation interruption")

                with self.assertRaises(RuntimeError):
                    self.converge(old_root, new_root, fault=fault)

                self.assertEqual(self.converge(old_root, new_root), new_root)
                self.assertFalse(old_root.exists())
                self.assertTrue(new_root.is_dir())
                self.assertEqual(
                    json.loads((new_root / "relocation.json").read_text())["phase"],
                    "complete",
                )

    def test_fault_after_one_metadata_replace_resumes_conversion_and_relocation(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            original_replace = root_module.os.replace
            interrupted = False

            def fail_after_one_metadata_replace(source, target):
                nonlocal interrupted
                original_replace(source, target)
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not interrupted
                    and ".v2-" in source_path.name
                    and target_path.resolve(strict=False).is_relative_to(
                        old_root.resolve(strict=False)
                    )
                ):
                    interrupted = True
                    raise OSError("injected after metadata replace")

            with mock.patch.object(
                root_module.os,
                "replace",
                side_effect=fail_after_one_metadata_replace,
            ):
                with self.assertRaises(OSError):
                    self.converge(old_root, new_root)

            self.assertTrue((old_root / "root.transaction.json").exists())
            self.assertEqual(self.converge(old_root, new_root), new_root)
            self.assertFalse(old_root.exists())
            self.assertEqual(
                json.loads((new_root / "root.json").read_text())["schema_version"],
                2,
            )

    def test_target_appearance_is_never_overwritten_or_copied(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)

            def race(_source: Path, target: Path) -> None:
                target.mkdir()
                (target / "foreign").write_text("keep\n")
                raise OSError(errno.EEXIST, "target appeared")

            with mock.patch.object(
                root_module,
                "_native_rename_noreplace",
                side_effect=race,
            ) as rename:
                with self.assertRaises(LoopMemoryError) as caught:
                    self.converge(old_root, new_root)

            self.assertEqual(caught.exception.code, "root_conflict")
            rename.assert_called_once_with(old_root, new_root)
            self.assertTrue(old_root.is_dir())
            self.assertEqual((new_root / "foreign").read_text(), "keep\n")
            self.assertFalse(old_root.is_symlink())
            self.assertFalse(new_root.is_symlink())

    def test_relocation_publishes_the_exact_phase_sequence(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            original_write = root_module.write_json_atomic
            phases = []

            def record_phase(path, value):
                if Path(path).name == "relocation.json":
                    phases.append(value["phase"])
                return original_write(path, value)

            with mock.patch.object(
                root_module,
                "write_json_atomic",
                side_effect=record_phase,
            ):
                self.assertEqual(self.converge(old_root, new_root), new_root)

            self.assertEqual(phases, [
                "validated",
                "conversion_prepared",
                "conversion_published",
                "root_renamed",
                "complete",
            ])

    def test_native_no_replace_rename_preserves_existing_target(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "source-data").write_text("source\n")
            (target / "target-data").write_text("target\n")

            with self.assertRaises(OSError) as caught:
                root_module._native_rename_noreplace(source, target)

            self.assertIn(caught.exception.errno, {errno.EEXIST, errno.ENOTEMPTY})
            self.assertEqual((source / "source-data").read_text(), "source\n")
            self.assertEqual((target / "target-data").read_text(), "target\n")

    def test_complete_new_root_journal_must_match_root_identity_without_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_new_root(new_root)
            transaction = new_root / "relocation.json"
            transaction.write_text(json.dumps({
                "schema_version": 1,
                "phase": "complete",
                "old_root": str(old_root),
                "new_root": str(new_root),
                "root_id": "r-forged-other",
            }, sort_keys=True) + "\n")
            root_metadata = new_root / "root.json"
            before = (root_metadata.read_bytes(), transaction.read_bytes())

            with self.assertRaises(LoopMemoryError) as caught:
                self.converge(old_root, new_root)

            self.assertEqual(caught.exception.code, "root_transaction_conflict")
            self.assertEqual(
                (root_metadata.read_bytes(), transaction.read_bytes()),
                before,
            )

    def test_concurrent_relocation_serializes_before_authority_check_and_rechecks_new(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            winner_paused = threading.Event()
            release_winner = threading.Event()
            results = {}

            def winner_fault(point: str) -> None:
                if point == "before_root_rename":
                    winner_paused.set()
                    if not release_winner.wait(timeout=5):
                        raise RuntimeError("winner release timed out")

            def run_winner() -> None:
                try:
                    results["winner"] = self.converge(
                        old_root,
                        new_root,
                        fault=winner_fault,
                    )
                except BaseException as error:  # pragma: no cover - assertion below
                    results["winner_error"] = error

            def run_loser() -> None:
                try:
                    results["loser"] = self.converge(old_root, new_root)
                except BaseException as error:  # pragma: no cover - assertion below
                    results["loser_error"] = error

            winner = threading.Thread(target=run_winner)
            winner.start()
            self.assertTrue(winner_paused.wait(timeout=5))

            loser = threading.Thread(target=run_loser)
            loser.start()
            time.sleep(0.1)
            self.assertTrue(loser.is_alive())
            self.assertNotIn("loser", results)

            release_winner.set()
            winner.join(timeout=5)
            loser.join(timeout=5)
            self.assertFalse(winner.is_alive())
            self.assertFalse(loser.is_alive())
            self.assertEqual(results.get("winner"), new_root)
            self.assertEqual(results.get("loser"), new_root)
            self.assertNotIn("winner_error", results)
            self.assertNotIn("loser_error", results)
            self.assertFalse(old_root.exists())
            self.assertTrue((new_root / "root.json").exists())
            self.assertFalse((home / ".loop-memory-relocation.lock").exists())

    def test_concurrent_prepublication_loser_never_recreates_renamed_old_root(self):
        from scripts.loopmem import root as root_module

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            old_root = home / ".codex" / "loop-memory"
            new_root = home / "loop-memory"
            self.make_old_root(old_root)
            loser_paused = threading.Event()
            release_loser = threading.Event()
            results = {}
            original_write = root_module._write_relocation

            def pause_loser(root, state, phase):
                if (
                    threading.current_thread().name == "relocation-loser"
                    and root == old_root
                    and phase == "conversion_published"
                    and not loser_paused.is_set()
                ):
                    loser_paused.set()
                    if not release_loser.wait(timeout=5):
                        raise RuntimeError("loser release timed out")
                return original_write(root, state, phase)

            def run(name: str) -> None:
                try:
                    results[name] = self.converge(old_root, new_root)
                except BaseException as error:  # pragma: no cover - assertion below
                    results[f"{name}_error"] = error

            with mock.patch.object(
                root_module,
                "_write_relocation",
                side_effect=pause_loser,
            ):
                loser = threading.Thread(
                    target=run,
                    args=("loser",),
                    name="relocation-loser",
                )
                loser.start()
                self.assertTrue(loser_paused.wait(timeout=5))
                winner = threading.Thread(
                    target=run,
                    args=("winner",),
                    name="relocation-winner",
                )
                winner.start()
                time.sleep(0.1)
                winner_waited = winner.is_alive()
                release_loser.set()
                loser.join(timeout=5)
                winner.join(timeout=5)

            self.assertTrue(winner_waited)
            self.assertEqual(results.get("loser"), new_root)
            self.assertEqual(results.get("winner"), new_root)
            self.assertNotIn("loser_error", results)
            self.assertNotIn("winner_error", results)
            self.assertFalse(old_root.exists())
            self.assertTrue((new_root / "root.json").exists())
            self.assertFalse((home / ".loop-memory-relocation.lock").exists())


if __name__ == "__main__":
    unittest.main()
