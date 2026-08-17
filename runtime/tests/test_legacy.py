import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem import legacy


BODY = b"LEGACY-BODY-\xff"


class LegacyCustodyTests(unittest.TestCase):
    def make_source(self, temp: Path, name: str = "project", body: bytes = BODY):
        project = temp / name
        source = project / ".memory"
        source.mkdir(parents=True)
        (source / "entry.bin").write_bytes(body)
        return project, source

    def test_stage_preserves_external_source_and_creates_verified_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, source = self.make_source(temp)
            before = source.stat()
            original = (source / "entry.bin").read_bytes()

            result = legacy.stage_legacy(temp / "loop", project)

            after = source.stat()
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
            self.assertEqual((source / "entry.bin").read_bytes(), original)
            payload = Path(result["snapshot_path"])
            self.assertEqual((payload / "entry.bin").read_bytes(), original)
            self.assertTrue(payload.is_relative_to(temp / "loop" / "legacy-snapshots"))
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            self.assertEqual(receipt["source_path"], str(source))
            self.assertEqual(receipt["inventory_sha256"], result["inventory_sha256"])
            self.assertTrue(receipt["importable"])

    def test_repeated_stage_reuses_same_source_and_inventory_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            first = legacy.stage_legacy(temp / "loop", project)
            second = legacy.stage_legacy(temp / "loop", project)
            self.assertEqual(second["snapshot_id"], first["snapshot_id"])
            self.assertEqual(second["receipt_path"], first["receipt_path"])

    def test_source_drift_during_copy_fails_and_removes_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, source = self.make_source(temp)
            real_copy = legacy._copy_source

            def drifting_copy(source_path, payload):
                real_copy(source_path, payload)
                (source / "entry.bin").write_bytes(b"changed")

            with mock.patch.object(legacy, "_copy_source", side_effect=drifting_copy):
                with self.assertRaises(LoopMemoryError) as context:
                    legacy.stage_legacy(temp / "loop", project)
            self.assertEqual(context.exception.code, "source_unstable")
            snapshots = temp / "loop" / "legacy-snapshots"
            self.assertEqual(list(snapshots.iterdir()), [])

    def test_stage_rejects_same_content_new_inode_swap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, source = self.make_source(temp)
            entry = source / "entry.bin"
            original_identity = (entry.stat().st_dev, entry.stat().st_ino)
            real_copy = legacy._copy_source

            def swapping_copy(source_path, payload):
                real_copy(source_path, payload)
                replacement = source / "replacement.bin"
                replacement.write_bytes(BODY)
                replacement.replace(entry)
                self.assertNotEqual(
                    (entry.stat().st_dev, entry.stat().st_ino),
                    original_identity,
                )

            with mock.patch.object(legacy, "_copy_source", side_effect=swapping_copy):
                with self.assertRaises(LoopMemoryError) as context:
                    legacy.stage_legacy(temp / "loop", project)

            self.assertEqual(context.exception.code, "source_unstable")
            snapshots = temp / "loop" / "legacy-snapshots"
            self.assertEqual(list(snapshots.iterdir()), [])

    def test_concurrent_stage_reuses_one_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            loop_root = temp / "loop"
            results = []
            errors = []

            def stage():
                try:
                    results.append(legacy.stage_legacy(loop_root, project))
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=stage) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["snapshot_id"], results[1]["snapshot_id"])
            snapshots = loop_root / "legacy-snapshots"
            self.assertEqual(
                len([path for path in snapshots.iterdir() if path.name.startswith("l-")]),
                1,
            )

    def test_credential_source_is_copied_but_marked_non_importable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, source = self.make_source(temp)
            (source / "entry.bin").write_bytes(b"SERVICE_TOKEN=secret\n")
            result = legacy.stage_legacy(temp / "loop", project)
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            self.assertFalse(receipt["importable"])
            self.assertEqual(receipt["protection_reasons"], ["credential_assignment"])
            self.assertTrue(source.is_dir())
            self.assertTrue(Path(result["snapshot_path"]).is_dir())

    def test_staging_never_invokes_external_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            with mock.patch("subprocess.run", side_effect=AssertionError("external call")):
                result = legacy.stage_legacy(temp / "loop", project)
            self.assertTrue(Path(result["snapshot_path"]).is_dir())

    def test_delete_removes_only_selected_internal_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            first_project, first_source = self.make_source(temp, "one")
            second_project, second_source = self.make_source(temp, "two", b"two")
            first = legacy.stage_legacy(temp / "loop", first_project)
            second = legacy.stage_legacy(temp / "loop", second_project)

            result = legacy.delete_legacy(temp / "loop", first["snapshot_id"])

            self.assertTrue(result["deleted"])
            self.assertFalse(Path(first["snapshot_path"]).exists())
            self.assertTrue(Path(second["snapshot_path"]).is_dir())
            self.assertTrue(first_source.is_dir())
            self.assertTrue(second_source.is_dir())

    def test_delete_never_removes_payload_replaced_after_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            staged = legacy.stage_legacy(temp / "loop", project)
            payload = Path(staged["snapshot_path"])
            displaced = payload.parent / "displaced-payload"
            real_validate = legacy._validate_payload
            swapped = False

            def swap_after_validation(candidate, digest):
                nonlocal swapped
                real_validate(candidate, digest)
                if Path(candidate) == payload and not swapped:
                    swapped = True
                    payload.rename(displaced)
                    payload.mkdir()
                    (payload / "attacker-owned.txt").write_text(
                        "must survive",
                        encoding="utf-8",
                    )

            with mock.patch.object(
                legacy,
                "_validate_payload",
                side_effect=swap_after_validation,
            ):
                with self.assertRaises(LoopMemoryError):
                    legacy.delete_legacy(temp / "loop", staged["snapshot_id"])

            self.assertEqual(
                (payload / "attacker-owned.txt").read_text(encoding="utf-8"),
                "must survive",
            )
            self.assertTrue(displaced.is_dir())
            self.assertTrue(Path(staged["receipt_path"]).is_file())

    def test_delete_never_removes_tombstone_replaced_after_rename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            staged = legacy.stage_legacy(temp / "loop", project)
            payload = Path(staged["snapshot_path"])
            real_verify = legacy._verify_delete_chain
            displaced_tombstone = None
            foreign_tombstone = None

            def swap_after_rename(*args, **kwargs):
                nonlocal displaced_tombstone, foreign_tombstone
                real_verify(*args, **kwargs)
                if kwargs.get("snapshot_metadata_may_change"):
                    snapshot_dir = payload.parent
                    tombstones = list(snapshot_dir.glob(".payload-delete-*"))
                    if tombstones and displaced_tombstone is None:
                        tombstone = tombstones[0]
                        displaced_tombstone = snapshot_dir / "held-original-tombstone"
                        tombstone.rename(displaced_tombstone)
                        tombstone.mkdir()
                        (tombstone / "foreign.txt").write_text(
                            "must survive",
                            encoding="utf-8",
                        )
                        foreign_tombstone = tombstone

            with mock.patch.object(
                legacy,
                "_verify_delete_chain",
                side_effect=swap_after_rename,
            ):
                with self.assertRaises(LoopMemoryError):
                    legacy.delete_legacy(temp / "loop", staged["snapshot_id"])

            self.assertIsNotNone(foreign_tombstone)
            self.assertEqual(
                (foreign_tombstone / "foreign.txt").read_text(encoding="utf-8"),
                "must survive",
            )
            self.assertTrue(displaced_tombstone.is_dir())
            self.assertTrue(Path(staged["receipt_path"]).is_file())

    def test_repeated_delete_is_idempotent_and_retains_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            staged = legacy.stage_legacy(temp / "loop", project)

            first = legacy.delete_legacy(temp / "loop", staged["snapshot_id"])
            second = legacy.delete_legacy(temp / "loop", staged["snapshot_id"])

            self.assertTrue(first["deleted"])
            self.assertFalse(second["deleted"])
            self.assertTrue(Path(staged["receipt_path"]).is_file())

    def test_delete_retry_finishes_identity_bound_tombstone_after_crash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            project, _ = self.make_source(temp)
            staged = legacy.stage_legacy(temp / "loop", project)
            real_remove = legacy._remove_tree_if_identity
            crashed = False

            def crash_before_tombstone_remove(path, identity):
                nonlocal crashed
                if Path(path).name.startswith(".payload-delete-") and not crashed:
                    crashed = True
                    raise RuntimeError("simulated crash after tombstone rename")
                return real_remove(path, identity)

            with mock.patch.object(
                legacy,
                "_remove_tree_if_identity",
                side_effect=crash_before_tombstone_remove,
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    legacy.delete_legacy(temp / "loop", staged["snapshot_id"])

            snapshot_dir = Path(staged["receipt_path"]).parent
            self.assertFalse(Path(staged["snapshot_path"]).exists())
            self.assertEqual(len(list(snapshot_dir.glob(".payload-delete-*"))), 1)

            retried = legacy.delete_legacy(temp / "loop", staged["snapshot_id"])

            self.assertTrue(retried["deleted"])
            self.assertEqual(list(snapshot_dir.glob(".payload-delete-*")), [])
            self.assertTrue(Path(staged["receipt_path"]).is_file())

    def test_delete_rejects_unknown_or_multiple_tombstones(self):
        for mode in ("unknown", "multiple"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir).resolve()
                project, _ = self.make_source(temp)
                staged = legacy.stage_legacy(temp / "loop", project)
                snapshot_dir = Path(staged["receipt_path"]).parent
                payload = Path(staged["snapshot_path"])
                payload.rename(snapshot_dir / ".payload-delete-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                if mode == "unknown":
                    (snapshot_dir / ".payload-delete-not-a-token").mkdir()
                else:
                    (snapshot_dir / ".payload-delete-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb").mkdir()

                with self.assertRaises(LoopMemoryError) as context:
                    legacy.delete_legacy(temp / "loop", staged["snapshot_id"])

                self.assertEqual(context.exception.code, "unsafe_legacy_snapshot")
                self.assertTrue(Path(staged["receipt_path"]).is_file())

    def test_delete_rejects_non_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(LoopMemoryError) as context:
                legacy.delete_legacy(Path(temp_dir) / "loop", "../external")
            self.assertEqual(context.exception.code, "invalid_legacy_snapshot_id")


if __name__ == "__main__":
    unittest.main()
