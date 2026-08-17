import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.loopmem import paths as paths_module
from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.paths import (
    ProjectDiscovery,
    assert_loop_path,
    default_loop_root,
    discover_project,
    is_reserved_product_path,
)


class LoopPathTests(unittest.TestCase):
    def test_default_loop_root_is_directly_under_home(self):
        home = Path("/") / "home" / "tester"

        with mock.patch.object(Path, "home", return_value=home):
            result = default_loop_root()

        self.assertEqual(result, home / "loop-memory")

    def test_legacy_loop_root_remains_under_codex_home(self):
        home = Path("/") / "home" / "tester"
        legacy_loop_root = getattr(paths_module, "legacy_loop_root", None)

        self.assertIsNotNone(legacy_loop_root)

        with mock.patch.object(Path, "home", return_value=home):
            result = legacy_loop_root()

        self.assertEqual(
            result,
            (home / ".codex" / "loop-memory").resolve(strict=False),
        )

    def test_containment_accepts_descendant_and_rejects_outside_and_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            descendant = loop_root / "project" / "status.md"

            self.assertEqual(
                assert_loop_path(loop_root, descendant),
                descendant.resolve(strict=False),
            )

            with self.assertRaises(LoopMemoryError) as outside_context:
                assert_loop_path(loop_root, temp / "outside.md")
            self.assertEqual(outside_context.exception.code, "path_outside_loop_root")
            self.assertIn("inside the loop root", outside_context.exception.message)

            with self.assertRaises(LoopMemoryError) as root_context:
                assert_loop_path(loop_root, loop_root)
            self.assertEqual(root_context.exception.code, "loop_root_not_file_target")
            self.assertIn("writable file target", root_context.exception.message)

    def test_containment_is_component_aware_for_shared_string_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            prefix_sibling = temp / "loop-backup" / "status.md"

            with self.assertRaises(LoopMemoryError) as context:
                assert_loop_path(loop_root, prefix_sibling)

            self.assertEqual(context.exception.code, "path_outside_loop_root")

    def test_containment_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            outside = temp / "outside"
            loop_root.mkdir()
            outside.mkdir()
            (loop_root / "escape").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(LoopMemoryError) as context:
                assert_loop_path(loop_root, loop_root / "escape" / "state.txt")

            self.assertEqual(context.exception.code, "path_outside_loop_root")


class ReservedProductPathTests(unittest.TestCase):
    def test_reserved_codex_product_path_families(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir).resolve() / "home" / ".codex"
            reserved = [
                codex_home / "memories",
                codex_home / "memories" / "archive" / "entry.json",
                codex_home / "memories_1.sqlite-wal",
                codex_home / "sqlite" / "memories_1.sqlite-shm",
            ]
            nearby_non_reserved = [
                codex_home / "memories-old",
                codex_home / "memories_2.sqlite",
                codex_home / "sqlite" / "nested" / "memories_1.sqlite",
            ]

            with mock.patch.object(Path, "home", return_value=codex_home.parent):
                for candidate in reserved:
                    with self.subTest(candidate=candidate):
                        self.assertIs(is_reserved_product_path(candidate), True)

                for candidate in nearby_non_reserved:
                    with self.subTest(candidate=candidate):
                        self.assertIs(is_reserved_product_path(candidate), False)

    def test_reserved_product_checks_are_lexical_and_filesystem_opaque(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir).resolve() / "home"
            product = home / ".codex" / "memories"
            candidates = (
                product,
                product / "archive" / "entry.json",
                product / ".." / "memories" / "entry.json",
                home / ".codex" / "memories_1.sqlite-wal",
                home / ".codex" / "sqlite" / "memories_1.sqlite-shm",
            )

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(
                    Path,
                    "resolve",
                    side_effect=AssertionError("product path was resolved"),
                ),
                mock.patch.object(
                    Path,
                    "lstat",
                    side_effect=AssertionError("product path was lstatted"),
                ),
                mock.patch.object(
                    Path,
                    "stat",
                    side_effect=AssertionError("product path was statted"),
                ),
            ):
                for candidate in candidates:
                    with self.subTest(candidate=candidate):
                        self.assertIs(is_reserved_product_path(candidate), True)


class ProjectDiscoveryTests(unittest.TestCase):
    def test_explicit_project_root_is_authoritative_and_no_external_discovery_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            nested = workspace / "src"
            nested.mkdir(parents=True)

            with mock.patch("subprocess.run", side_effect=AssertionError("external call")):
                discovery = discover_project(nested, project_root=workspace)

            self.assertEqual(discovery.kind, "directory")
            self.assertEqual(discovery.cwd, nested.resolve(strict=False))
            self.assertEqual(discovery.root, workspace.resolve(strict=False))
            self.assertIsNone(discovery.alias)

    def test_default_identity_is_cwd_and_does_not_search_parents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            nested = Path(temp_dir) / "parent" / "nested"
            nested.mkdir(parents=True)

            with mock.patch("subprocess.run", side_effect=AssertionError("external call")):
                discovery = discover_project(nested)

            self.assertEqual(discovery.root, nested.resolve(strict=False))

    def test_explicit_project_root_must_contain_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            workspace = temp / "workspace"
            outside = temp / "outside"
            workspace.mkdir()
            outside.mkdir()

            with self.assertRaises(LoopMemoryError) as context:
                discover_project(outside, project_root=workspace)

            self.assertEqual(context.exception.code, "cwd_outside_project_root")

    def test_non_directory_defaults_to_normalized_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "file.txt"
            candidate.write_text("not a directory\n", encoding="utf-8")

            discovery = discover_project(candidate)

            self.assertEqual(
                discovery,
                ProjectDiscovery(
                    kind="directory",
                    cwd=candidate.resolve(strict=False),
                    root=candidate.resolve(strict=False),
                    alias=None,
                ),
            )


if __name__ == "__main__":
    unittest.main()
