import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError
from scripts.loopmem.paths import ProjectDiscovery


INITIAL_STATE = {
    "schema_version": 1,
    "projects": {},
    "sessions": {},
    "legacy_aliases": {},
    "maintenance": {},
}


class RegistryStoreTests(unittest.TestCase):
    def registry_store_class(self):
        try:
            module = importlib.import_module("scripts.loopmem.registry")
        except ModuleNotFoundError:
            self.fail("scripts.loopmem.registry has not been implemented")
        return module.RegistryStore

    def discovery(
        self,
        *,
        kind="directory",
        root: Path,
        alias: str | None = None,
    ) -> ProjectDiscovery:
        return ProjectDiscovery(
            kind=kind,
            cwd=root,
            root=root,
            alias=alias,
        )

    def write_state(self, loop_root: Path, state: dict[str, object]) -> bytes:
        content = json.dumps(state, sort_keys=True).encode("utf-8") + b"\n"
        (loop_root / "registry.json").write_bytes(content)
        return content

    def test_initialize_creates_version_one_layout_with_system_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop-memory"

            self.registry_store_class()(loop_root).initialize()

            registry_path = loop_root / "registry.json"
            self.assertEqual(
                json.loads(registry_path.read_text(encoding="utf-8")),
                INITIAL_STATE,
            )
            self.assertTrue(loop_root.is_dir())
            self.assertTrue((loop_root / "locks").is_dir())
            self.assertTrue(registry_path.is_file())

    def test_public_validate_checks_registry_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop-memory"
            store = self.registry_store_class()(loop_root)
            store.initialize()
            registry_path = loop_root / "registry.json"
            before = registry_path.read_bytes()

            self.assertIsNone(store.validate())

            self.assertEqual(registry_path.read_bytes(), before)

    def test_version_two_generation_is_monotonic_and_registry_is_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop"
            root.mkdir()
            state = {
                "schema_version": 2,
                "generation": 7,
                "projects": {
                    "p-one": {"roots": [str((root.parent / "project").resolve())]},
                },
                "sessions": {
                    "s-one": {
                        "project_id": "p-one",
                        "platform_session_id": "host-one",
                        "generation": 1,
                        "resumes_from": None,
                        "state": "active",
                    }
                },
                "legacy_aliases": {},
                "maintenance": {},
            }
            self.write_state(root, state)
            store = self.registry_store_class()(root)
            self.assertIsNone(store.validate())
            before = (root / "registry.json").read_bytes()
            self.assertIsNone(store.validate())
            self.assertEqual((root / "registry.json").read_bytes(), before)

            lowered = dict(state, generation=6)
            self.write_state(root, lowered)
            with self.assertRaises(LoopMemoryError) as caught:
                store.validate(minimum_generation=7)
            self.assertEqual(caught.exception.code, "registry_generation_regressed")

    def test_explicit_v2_initialization_starts_generation_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop"
            store = self.registry_store_class()(root)
            store.initialize_v2()
            state = json.loads((root / "registry.json").read_text())
            self.assertEqual(state, {
                "schema_version": 2,
                "generation": 1,
                "projects": {},
                "sessions": {},
                "legacy_aliases": {},
                "maintenance": {},
            })

    def test_v2_store_resolves_root_and_platform_session_without_git_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop"
            root.mkdir()
            project_root = (root.parent / "project").resolve()
            self.write_state(root, {
                "schema_version": 2,
                "generation": 4,
                "projects": {"p-one": {"roots": [str(project_root)]}},
                "sessions": {"s-one": {
                    "project_id": "p-one",
                    "platform_session_id": "host-one",
                    "generation": 1,
                    "resumes_from": None,
                    "state": "active",
                }},
                "legacy_aliases": {},
                "maintenance": {},
            })
            store = self.registry_store_class()(root)
            self.assertEqual(
                store.resolve_project(
                    self.discovery(kind="directory", root=project_root),
                    create=False,
                ),
                "p-one",
            )
            self.assertEqual(
                store.resolve_session("p-one", "host-one", create=False),
                "s-one",
            )

    def test_v2_archived_platform_session_creates_one_successor_generation(self):
        """A resumed host session gets one new registry generation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            store = self.registry_store_class()(root, id_factory=iter(("p", "s1", "s2")).__next__)
            store.initialize_v2()
            project_id = store.resolve_project(self.discovery(root=project_root), create=True)
            first = store.resolve_session_info(project_id, "platform", create=True)
            active = root / "projects" / project_id / "sessions" / "active" / first["session_id"]
            archive = root / "projects" / project_id / "sessions" / "archive" / "2026-08" / first["session_id"]
            active.mkdir(parents=True)
            archive.parent.mkdir(parents=True)
            active.rename(archive)
            second = store.resolve_session_info(project_id, "platform", create=True)
            (root / "projects" / project_id / "sessions" / "active" / second["session_id"]).mkdir(parents=True)
            self.assertEqual(second["session_generation"], 2)
            self.assertEqual(second["resumes_from"], first["session_id"])
            repeated = store.resolve_session_info(project_id, "platform", create=True)
            self.assertEqual(repeated["session_id"], second["session_id"])
            self.assertEqual(repeated["session_generation"], 2)

    def test_v2_new_session_materializes_under_registry_lease_before_publish(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            suffixes = iter(("project", "session"))
            store = self.registry_store_class()(root, id_factory=lambda: next(suffixes))
            store.initialize_v2()
            project_id = store.resolve_project(self.discovery(root=project_root), create=True)
            observed: list[tuple[str, bool, bool]] = []

            def materialize(session_id: str) -> None:
                state = json.loads(store.registry_path.read_text())
                observed.append((session_id, store.lease_path.exists(), session_id in state["sessions"]))
                (root / "projects" / project_id / "sessions" / "active" / session_id).mkdir(parents=True)

            info = store.resolve_session_info(
                project_id, "host", create=True, materialize_active=materialize
            )

            self.assertEqual(observed, [(info["session_id"], True, False)])
            self.assertTrue((root / "projects" / project_id / "sessions" / "active" / info["session_id"]).is_dir())

    def test_v2_archive_recovery_after_registry_update_failure_creates_at_most_one_successor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            suffixes = iter(("p", "one", "two"))
            store = self.registry_store_class()(root, id_factory=lambda: next(suffixes))
            store.initialize_v2()
            project_id = store.resolve_project(self.discovery(root=project_root), create=True)
            first = store.resolve_session_info(project_id, "platform", create=True)
            from scripts.loopmem.sessions import ensure_session_layout, archive_session
            ensure_session_layout(root, project_id, first["session_id"])
            archive_session(root, project_id, first["session_id"])
            registry_after_failed_close_commit = (root / "registry.json").read_bytes()

            second = store.resolve_session_info(project_id, "platform", create=True)
            (root / "projects" / project_id / "sessions" / "active" / second["session_id"]).mkdir(parents=True)
            repeated = store.resolve_session_info(project_id, "platform", create=True)

            self.assertNotEqual((root / "registry.json").read_bytes(), registry_after_failed_close_commit)
            self.assertEqual(second["session_id"], repeated["session_id"])
            self.assertEqual(second["session_generation"], 2)
            state = json.loads((root / "registry.json").read_text())
            self.assertEqual(len(state["sessions"]), 2)

    def test_v2_active_archive_conflict_fails_without_registry_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            suffixes = iter(("p", "one"))
            store = self.registry_store_class()(root, id_factory=lambda: next(suffixes))
            store.initialize_v2()
            project_id = store.resolve_project(self.discovery(root=project_root), create=True)
            first = store.resolve_session_info(project_id, "platform", create=True)
            active = root / "projects" / project_id / "sessions" / "active" / first["session_id"]
            archive = root / "projects" / project_id / "sessions" / "archive/2026-08" / first["session_id"]
            active.mkdir(parents=True)
            archive.mkdir(parents=True)
            before = (root / "registry.json").read_bytes()

            with self.assertRaises(LoopMemoryError) as caught:
                store.resolve_session_info(project_id, "platform", create=True)

            self.assertEqual(caught.exception.code, "ambiguous_session")
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_three_generation_lineage_requires_every_predecessor_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            root.mkdir()
            project_id = "p-project"
            active = root / "projects" / project_id / "sessions" / "active/s-three"
            archive_two = root / "projects" / project_id / "sessions" / "archive/2026-08/s-two"
            active.mkdir(parents=True)
            archive_two.mkdir(parents=True)
            self.write_state(root, {
                "schema_version": 2,
                "generation": 7,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "archived"},
                    "s-two": {"project_id": project_id, "platform_session_id": "host", "generation": 2, "resumes_from": "s-one", "state": "archived"},
                    "s-three": {"project_id": project_id, "platform_session_id": "host", "generation": 3, "resumes_from": "s-two", "state": "active"},
                },
                "legacy_aliases": {},
                "maintenance": {},
            })
            store = self.registry_store_class()(root)
            before = (root / "registry.json").read_bytes()

            with self.assertRaises(LoopMemoryError) as caught:
                store.resolve_session_info(project_id, "host", create=True)

            self.assertEqual(caught.exception.code, "ambiguous_session")
            self.assertEqual((root / "registry.json").read_bytes(), before)
            self.assertTrue(active.is_dir())
            self.assertTrue(archive_two.is_dir())

    def test_v2_valid_three_generation_lineage_reuses_newest_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            root.mkdir()
            project_id = "p-project"
            for relative in (
                "projects/p-project/sessions/archive/2026-07/s-one",
                "projects/p-project/sessions/archive/2026-08/s-two",
                "projects/p-project/sessions/active/s-three",
            ):
                (root / relative).mkdir(parents=True)
            state = {
                "schema_version": 2,
                "generation": 7,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "archived"},
                    "s-two": {"project_id": project_id, "platform_session_id": "host", "generation": 2, "resumes_from": "s-one", "state": "archived"},
                    "s-three": {"project_id": project_id, "platform_session_id": "host", "generation": 3, "resumes_from": "s-two", "state": "active"},
                },
                "legacy_aliases": {},
                "maintenance": {},
            }
            before = self.write_state(root, state)

            info = self.registry_store_class()(root).resolve_session_info(
                project_id, "host", create=True
            )

            self.assertEqual(info["session_id"], "s-three")
            self.assertEqual(info["session_generation"], 3)
            self.assertEqual(info["resumes_from"], "s-two")
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_three_generation_duplicate_old_archive_fails_without_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            project_id = "p-project"
            for relative in (
                "projects/p-project/sessions/archive/2026-07/s-one",
                "projects/p-project/sessions/archive/2026-08/s-one",
                "projects/p-project/sessions/archive/2026-08/s-two",
                "projects/p-project/sessions/active/s-three",
            ):
                (root / relative).mkdir(parents=True)
            before = self.write_state(root, {
                "schema_version": 2, "generation": 7,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "archived"},
                    "s-two": {"project_id": project_id, "platform_session_id": "host", "generation": 2, "resumes_from": "s-one", "state": "archived"},
                    "s-three": {"project_id": project_id, "platform_session_id": "host", "generation": 3, "resumes_from": "s-two", "state": "active"},
                }, "legacy_aliases": {}, "maintenance": {},
            })

            with self.assertRaises(LoopMemoryError) as caught:
                self.registry_store_class()(root).resolve_session_info(
                    project_id, "host", create=True
                )

            self.assertEqual(caught.exception.code, "ambiguous_session")
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_missing_latest_active_without_archive_fails_without_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            project_id = "p-project"
            (root / "projects/p-project/sessions/archive/2026-08/s-one").mkdir(parents=True)
            before = self.write_state(root, {
                "schema_version": 2, "generation": 5,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "archived"},
                    "s-two": {"project_id": project_id, "platform_session_id": "host", "generation": 2, "resumes_from": "s-one", "state": "active"},
                }, "legacy_aliases": {}, "maintenance": {},
            })

            with self.assertRaises(LoopMemoryError) as caught:
                self.registry_store_class()(root).resolve_session_info(
                    project_id, "host", create=True
                )

            self.assertEqual(caught.exception.code, "ambiguous_session")
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_missing_latest_active_without_archive_reinitializes_same_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            project_id = "p-project"
            (root / "projects/p-project/sessions/archive/2026-08/s-one").mkdir(parents=True)
            self.write_state(root, {
                "schema_version": 2,
                "generation": 5,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {
                        "project_id": project_id,
                        "platform_session_id": "host",
                        "generation": 1,
                        "resumes_from": None,
                        "state": "archived",
                    },
                    "s-two": {
                        "project_id": project_id,
                        "platform_session_id": "host",
                        "generation": 2,
                        "resumes_from": "s-one",
                        "state": "active",
                    },
                },
                "legacy_aliases": {},
                "maintenance": {},
            })

            def materialize(session_id: str):
                active = root / "projects" / project_id / "sessions" / "active" / session_id
                active.mkdir(parents=True)

            info = self.registry_store_class()(root).resolve_session_info(
                project_id,
                "host",
                create=True,
                materialize_active=materialize,
            )

            self.assertEqual(info["session_id"], "s-two")
            self.assertEqual(info["session_generation"], 2)
            self.assertTrue(info["session_recovered"])
            self.assertTrue(
                (root / "projects" / project_id / "sessions" / "active" / "s-two").is_dir()
            )

    def test_v2_missing_active_recovery_rolls_back_tree_when_registry_publish_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            project_id = "p-project"
            (root / "projects/p-project/sessions/archive/2026-08/s-one").mkdir(parents=True)
            before = self.write_state(root, {
                "schema_version": 2,
                "generation": 5,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {
                        "project_id": project_id,
                        "platform_session_id": "host",
                        "generation": 1,
                        "resumes_from": None,
                        "state": "archived",
                    },
                    "s-two": {
                        "project_id": project_id,
                        "platform_session_id": "host",
                        "generation": 2,
                        "resumes_from": "s-one",
                        "state": "active",
                    },
                },
                "legacy_aliases": {},
                "maintenance": {},
            })
            module = importlib.import_module("scripts.loopmem.registry")

            with mock.patch.object(
                module,
                "write_json_atomic",
                side_effect=OSError("injected registry publish failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected registry publish failure"):
                    self.registry_store_class()(root).resolve_session_info(
                        project_id,
                        "host",
                        create=True,
                        materialize_active=lambda session_id: (
                            (root / "projects" / project_id / "sessions" / "active" / session_id).mkdir(parents=True)
                        ),
                    )

            active_parent = root / "projects" / project_id / "sessions" / "active"
            self.assertEqual(list(active_parent.iterdir()), [])
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_archive_month_symlink_never_escapes_loop_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            outside = temp / "outside"
            project_root.mkdir()
            (outside / "s-one").mkdir(parents=True)
            project_id = "p-project"
            archive_root = root / "projects/p-project/sessions/archive"
            archive_root.mkdir(parents=True)
            (archive_root / "2026-08").symlink_to(outside, target_is_directory=True)
            before = self.write_state(root, {
                "schema_version": 2, "generation": 3,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "active"},
                }, "legacy_aliases": {}, "maintenance": {},
            })

            with self.assertRaises(LoopMemoryError) as caught:
                self.registry_store_class()(root).resolve_session_info(
                    project_id, "host", create=True
                )

            self.assertEqual(caught.exception.code, "ambiguous_session")
            self.assertNotIn(str(outside), caught.exception.message)
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_archive_ancestor_symlinks_never_escape_loop_root(self):
        ancestors = ("projects", "project", "sessions", "archive")
        for symlink_at in ancestors:
            with self.subTest(symlink_at=symlink_at), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                root = temp / "loop"
                project_root = temp / "project-work"
                outside = temp / "outside"
                project_root.mkdir()
                outside.mkdir()
                project_id = "p-project"
                components = ("projects", project_id, "sessions", "archive")
                names = {"projects": "projects", "project": project_id, "sessions": "sessions", "archive": "archive"}
                target_name = names[symlink_at]
                current = root
                outside_target = outside / target_name
                outside_target.mkdir(parents=True)
                for component in components:
                    candidate = current / component
                    if component == target_name:
                        candidate.parent.mkdir(parents=True, exist_ok=True)
                        candidate.symlink_to(outside_target, target_is_directory=True)
                        current = outside_target
                    else:
                        candidate.mkdir(parents=True, exist_ok=True)
                        current = candidate
                (current / "2026-08/s-one").mkdir(parents=True)
                before = self.write_state(root, {
                    "schema_version": 2, "generation": 3,
                    "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                    "sessions": {
                        "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "active"},
                    }, "legacy_aliases": {}, "maintenance": {},
                })

                with self.assertRaises(LoopMemoryError) as caught:
                    self.registry_store_class()(root).resolve_session_info(
                        project_id, "host", create=True
                    )

                self.assertEqual(caught.exception.code, "ambiguous_session")
                self.assertNotIn(str(outside), caught.exception.message)
                self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_archive_month_swap_to_symlink_is_detected_without_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            outside = temp / "outside"
            project_root.mkdir()
            (outside / "s-one").mkdir(parents=True)
            project_id = "p-project"
            month = root / "projects/p-project/sessions/archive/2026-08"
            (month / "s-one").mkdir(parents=True)
            before = self.write_state(root, {
                "schema_version": 2, "generation": 3,
                "projects": {project_id: {"roots": [str(project_root.resolve())]}},
                "sessions": {
                    "s-one": {"project_id": project_id, "platform_session_id": "host", "generation": 1, "resumes_from": None, "state": "active"},
                }, "legacy_aliases": {}, "maintenance": {},
            })
            module = importlib.import_module("scripts.loopmem.registry")
            real_open = module.os.open
            swapped = False

            def swap_before_month_open(path, *args, **kwargs):
                nonlocal swapped
                if str(path) == "2026-08" and not swapped:
                    swapped = True
                    renamed = month.with_name("2026-08-original")
                    month.rename(renamed)
                    month.symlink_to(outside, target_is_directory=True)
                return real_open(path, *args, **kwargs)

            with mock.patch.object(module.os, "open", side_effect=swap_before_month_open):
                with self.assertRaises(LoopMemoryError) as caught:
                    module.RegistryStore(root).resolve_session_info(
                        project_id, "host", create=True
                    )

            self.assertTrue(swapped)
            self.assertEqual(caught.exception.code, "ambiguous_session")
            self.assertNotIn(str(outside), caught.exception.message)
            self.assertEqual((root / "registry.json").read_bytes(), before)

    def test_v2_registry_publish_failure_rolls_back_new_empty_session_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            suffixes = iter(("project", "one", "two"))
            module = importlib.import_module("scripts.loopmem.registry")
            store = module.RegistryStore(root, id_factory=lambda: next(suffixes))
            store.initialize_v2()
            project_id = store.resolve_project(self.discovery(root=project_root), create=True)
            real_write = module.write_json_atomic
            failed = False

            def fail_session_publish(path, state):
                nonlocal failed
                if state.get("sessions") and not failed:
                    failed = True
                    raise OSError("injected registry publish failure")
                return real_write(path, state)

            with mock.patch.object(module, "write_json_atomic", side_effect=fail_session_publish):
                with self.assertRaisesRegex(OSError, "injected registry publish failure"):
                    store.resolve_session_info(
                        project_id,
                        "host",
                        create=True,
                        materialize_active=lambda session_id: (
                            (root / "projects" / project_id / "sessions" / "active" / session_id).mkdir(parents=True)
                        ),
                    )

            active = root / "projects" / project_id / "sessions" / "active"
            self.assertEqual(list(active.iterdir()), [])
            info = store.resolve_session_info(
                project_id,
                "host",
                create=True,
                materialize_active=lambda session_id: (
                    (root / "projects" / project_id / "sessions" / "active" / session_id).mkdir(parents=True)
                ),
            )
            self.assertEqual(len(list(active.iterdir())), 1)
            state = json.loads((root / "registry.json").read_text())
            self.assertEqual(set(state["sessions"]), {info["session_id"]})

    def test_v2_registry_publish_failure_preserves_foreign_nonempty_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            project_root = temp / "project"
            project_root.mkdir()
            suffixes = iter(("project", "one"))
            module = importlib.import_module("scripts.loopmem.registry")
            store = module.RegistryStore(root, id_factory=lambda: next(suffixes))
            store.initialize_v2()
            project_id = store.resolve_project(self.discovery(root=project_root), create=True)
            real_write = module.write_json_atomic
            injected = False

            def fail_publish_and_replace(path, state):
                nonlocal injected
                if state.get("sessions") and not injected:
                    injected = True
                    session_id = next(iter(state["sessions"]))
                    active = root / "projects" / project_id / "sessions" / "active" / session_id
                    (active / "foreign.md").write_text("foreign\n")
                    raise OSError("injected registry publish failure")
                return real_write(path, state)

            with mock.patch.object(module, "write_json_atomic", side_effect=fail_publish_and_replace):
                with self.assertRaisesRegex(OSError, "injected registry publish failure"):
                    store.resolve_session_info(
                        project_id, "host", create=True,
                        materialize_active=lambda session_id: (
                            (root / "projects" / project_id / "sessions" / "active" / session_id).mkdir(parents=True)
                        ),
                    )

            active = root / "projects" / project_id / "sessions" / "active"
            self.assertEqual(len(list(active.iterdir())), 1)
            self.assertEqual((next(active.iterdir()) / "foreign.md").read_text(), "foreign\n")

    def test_v2_directory_move_requires_explicit_alias_and_records_new_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "loop"
            store = self.registry_store_class()(root, id_factory=lambda: "new")
            store.initialize_v2()
            old = temp / "old"
            moved = temp / "moved"
            project_id = store.resolve_project(
                self.discovery(root=old), create=True
            )
            self.assertIsNone(store.resolve_project(self.discovery(root=moved), create=False))
            self.assertEqual(
                store.resolve_project(
                    self.discovery(root=moved, alias=str(old)), create=True
                ),
                project_id,
            )
            state = json.loads((root / "registry.json").read_text())
            self.assertEqual(
                state["projects"][project_id]["roots"],
                [str(old.resolve()), str(moved.resolve())],
            )

    def test_relative_loop_root_is_anchored_at_construction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            original_cwd = Path.cwd()
            construction_cwd = temp / "construction"
            later_cwd = temp / "later"
            construction_cwd.mkdir()
            later_cwd.mkdir()
            expected_root = (construction_cwd / "relative-loop").resolve(strict=False)

            try:
                os.chdir(construction_cwd)
                store = self.registry_store_class()(
                    Path("relative-loop"),
                    id_factory=lambda: "one",
                )
                os.chdir(later_cwd)
                store.initialize()
                project_id = store.resolve_project(
                    self.discovery(
                        kind="directory",
                        root=temp / "project",
                    ),
                    create=True,
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(store.loop_root, expected_root)
            self.assertEqual(store.registry_path, expected_root / "registry.json")
            self.assertEqual(
                store.lease_path,
                expected_root / "locks" / "registry.lock",
            )
            self.assertEqual(project_id, "p-one")
            self.assertTrue((expected_root / "registry.json").is_file())
            self.assertFalse((later_cwd / "relative-loop").exists())

    def test_persisted_path_validation_does_not_reresolve_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            original_target = temp / "original-project"
            moved_target = temp / "moved-project"
            replacement_target = temp / "replacement-project"
            input_link = temp / "project-link"
            original_target.mkdir()
            replacement_target.mkdir()
            input_link.symlink_to(original_target, target_is_directory=True)
            store = self.registry_store_class()(loop_root, id_factory=lambda: "one")
            store.initialize()

            project_id = store.resolve_project(
                self.discovery(kind="directory", root=input_link),
                create=True,
            )
            registry_path = loop_root / "registry.json"
            original = registry_path.read_bytes()
            stored_root = json.loads(original)["projects"][project_id]["roots"][0]
            self.assertEqual(stored_root, str(original_target.resolve(strict=False)))

            input_link.unlink()
            original_target.rename(moved_target)
            original_target.symlink_to(replacement_target, target_is_directory=True)

            self.assertIsNone(store.resolve_legacy_alias(temp / "unrelated"))
            self.assertEqual(registry_path.read_bytes(), original)

    def test_same_basename_directories_get_distinct_project_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            suffixes = iter(("one", "two"))
            store = self.registry_store_class()(
                temp / "loop",
                id_factory=lambda: next(suffixes),
            )
            store.initialize()

            first_id = store.resolve_project(
                self.discovery(
                    kind="directory",
                    root=temp / "first" / "service",
                ),
                create=True,
            )
            second_id = store.resolve_project(
                self.discovery(
                    kind="directory",
                    root=temp / "second" / "service",
                ),
                create=True,
            )

            self.assertEqual((first_id, second_id), ("p-one", "p-two"))

    def test_missing_project_read_does_not_mutate_registry_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            store = self.registry_store_class()(loop_root)
            store.initialize_v2()
            original = (loop_root / "registry.json").read_bytes()

            project_id = store.resolve_project(
                self.discovery(
                    kind="directory",
                    root=temp / "unregistered",
                ),
                create=False,
            )

            self.assertIsNone(project_id)
            self.assertEqual((loop_root / "registry.json").read_bytes(), original)

    def test_host_thread_restores_session_with_project_scoping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            suffixes = iter(("project-one", "project-two", "session-one", "session-two"))
            store = self.registry_store_class()(
                temp / "loop",
                id_factory=lambda: next(suffixes),
            )
            store.initialize()
            first_project = store.resolve_project(
                self.discovery(kind="directory", root=temp / "first"),
                create=True,
            )
            second_project = store.resolve_project(
                self.discovery(kind="directory", root=temp / "second"),
                create=True,
            )

            before_absent_read = (temp / "loop" / "registry.json").read_bytes()
            self.assertIsNone(
                store.resolve_session(first_project, "host-thread", create=False)
            )
            self.assertEqual(
                (temp / "loop" / "registry.json").read_bytes(),
                before_absent_read,
            )
            first_session = store.resolve_session(
                first_project,
                "host-thread",
                create=True,
            )
            restored = store.resolve_session(
                first_project,
                "host-thread",
                create=True,
            )
            second_session = store.resolve_session(
                second_project,
                "host-thread",
                create=True,
            )

            self.assertEqual(first_session, "s-session-one")
            self.assertEqual(restored, first_session)
            self.assertEqual(second_session, "s-session-two")
            sessions = json.loads(
                (temp / "loop" / "registry.json").read_text(encoding="utf-8")
            )["sessions"]
            self.assertEqual(
                sessions,
                {
                    "s-session-one": {
                        "project_id": "p-project-one",
                        "thread_id": "host-thread",
                    },
                    "s-session-two": {
                        "project_id": "p-project-two",
                        "thread_id": "host-thread",
                    },
                },
            )

    def test_unknown_project_session_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            store = self.registry_store_class()(loop_root, id_factory=lambda: "unused")
            store.initialize()
            original = (loop_root / "registry.json").read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                store.resolve_session("p-missing", "host-thread", create=True)

            self.assertEqual(context.exception.code, "unknown_project")
            self.assertEqual((loop_root / "registry.json").read_bytes(), original)

    def test_duplicate_session_mapping_is_ambiguous_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            store = self.registry_store_class()(loop_root)
            store.initialize()
            project_root = str((temp / "project").resolve(strict=False))
            state = dict(INITIAL_STATE)
            state["projects"] = {
                "p-project": {
                    "kind": "directory",
                    "shared_roots": [],
                    "roots": [project_root],
                    "upstream_refs": [],
                }
            }
            state["sessions"] = {
                "s-one": {
                    "project_id": "p-project",
                    "thread_id": "host-thread",
                },
                "s-two": {
                    "project_id": "p-project",
                    "thread_id": "host-thread",
                },
            }
            original = self.write_state(loop_root, state)

            with self.assertRaises(LoopMemoryError) as context:
                store.resolve_session("p-project", "host-thread", create=True)

            self.assertEqual(context.exception.code, "ambiguous_session")
            self.assertEqual((loop_root / "registry.json").read_bytes(), original)

    def test_local_sessions_are_always_new_and_not_restored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            suffixes = iter(("project", "local-one", "local-two"))
            store = self.registry_store_class()(
                temp / "loop",
                id_factory=lambda: next(suffixes),
            )
            store.initialize()
            project_id = store.resolve_project(
                self.discovery(kind="directory", root=temp / "project"),
                create=True,
            )

            self.assertIsNone(
                store.resolve_session(project_id, None, create=False)
            )
            first = store.resolve_session(project_id, None, create=True)
            second = store.resolve_session(project_id, None, create=True)

            self.assertEqual((first, second), ("s-local-one", "s-local-two"))

    def test_legacy_alias_add_resolve_idempotence_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            store = self.registry_store_class()(loop_root)
            store.initialize()
            legacy_path = temp / "legacy" / ".." / "legacy" / "status.md"
            lexical = Path(os.path.abspath(legacy_path.expanduser()))
            normalized = str(lexical.parent.resolve(strict=False) / lexical.name)

            self.assertIsNone(store.resolve_legacy_alias(legacy_path))
            store.add_legacy_alias(legacy_path, "projects/p-one", "migration-one")
            resolved = store.resolve_legacy_alias(legacy_path)

            self.assertEqual(
                resolved,
                {"target": "projects/p-one", "migration_id": "migration-one"},
            )
            self.assertEqual(
                list(
                    json.loads(
                        (loop_root / "registry.json").read_text(encoding="utf-8")
                    )["legacy_aliases"]
                ),
                [normalized],
            )
            resolved["target"] = "changed-locally"
            self.assertEqual(
                store.resolve_legacy_alias(legacy_path),
                {"target": "projects/p-one", "migration_id": "migration-one"},
            )

            before_idempotent = (loop_root / "registry.json").read_bytes()
            store.add_legacy_alias(legacy_path, "projects/p-one", "migration-one")
            self.assertEqual(
                (loop_root / "registry.json").read_bytes(),
                before_idempotent,
            )

            for target, migration_id in (
                ("projects/p-two", "migration-one"),
                ("projects/p-one", "migration-two"),
            ):
                with self.subTest(target=target, migration_id=migration_id):
                    with self.assertRaises(LoopMemoryError) as context:
                        store.add_legacy_alias(legacy_path, target, migration_id)
                    self.assertEqual(context.exception.code, "legacy_alias_conflict")
                    self.assertEqual(
                        (loop_root / "registry.json").read_bytes(),
                        before_idempotent,
                    )

    def test_v2_legacy_alias_rewrites_equivalent_absolute_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            store = self.registry_store_class()(loop_root)
            store.initialize_v2()
            legacy_path = temp / "legacy" / ".memory"
            legacy_key = str(legacy_path.resolve(strict=False))
            registry_path = loop_root / "registry.json"
            state = json.loads(registry_path.read_text(encoding="utf-8"))
            state["legacy_aliases"][legacy_key] = {
                "target": str(loop_root / "projects/p-one"),
                "migration_id": "m-00000000000000000000000000000000",
            }
            registry_path.write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            store.add_legacy_alias(
                legacy_path,
                str(loop_root / "projects/p-one"),
                "m-00000000000000000000000000000000",
            )

            rewritten = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(
                rewritten["legacy_aliases"][legacy_key]["target"],
                "projects/p-one",
            )
            self.assertEqual(rewritten["generation"], state["generation"] + 1)

    def test_v2_legacy_alias_normalizes_equivalent_absolute_internal_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            loop_root = temp / "loop"
            store = self.registry_store_class()(loop_root)
            store.initialize_v2()
            legacy_path = temp / "project" / ".memory"
            target = loop_root / "projects" / "p-one"
            state_path = loop_root / "registry.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            key = str(legacy_path.resolve(strict=False))
            state["legacy_aliases"][key] = {
                "target": str(target),
                "migration_id": "migration-one",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")

            store.add_legacy_alias(
                legacy_path,
                str(target),
                "migration-one",
            )

            repaired = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                repaired["legacy_aliases"][key]["target"],
                "projects/p-one",
            )

    def test_legacy_alias_keys_are_lexical_and_do_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            real_source = temp / "real" / ".memory"
            real_source.mkdir(parents=True)
            legacy_link = temp / "project" / ".memory"
            legacy_link.parent.mkdir()
            legacy_link.symlink_to(real_source, target_is_directory=True)
            store = self.registry_store_class()(loop_root)
            store.initialize()

            store.add_legacy_alias(
                legacy_link,
                "projects/p-one",
                "migration-one",
            )

            state = json.loads(
                (loop_root / "registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                list(state["legacy_aliases"]),
                [str(legacy_link.parent.resolve(strict=False) / legacy_link.name)],
            )
            self.assertEqual(
                store.resolve_legacy_alias(legacy_link),
                {
                    "target": "projects/p-one",
                    "migration_id": "migration-one",
                },
            )
            self.assertIsNone(store.resolve_legacy_alias(real_source))

    def test_legacy_alias_equates_var_with_private_var_without_resolving_final_component(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir).resolve() / "loop"
            store = self.registry_store_class()(loop_root)
            store.initialize()
            lexical_parent = Path("/var/folders/safe-project")
            canonical_parent = Path("/private/var/folders/safe-project")
            lexical_source = lexical_parent / ".memory"
            canonical_source = canonical_parent / ".memory"
            resolved: list[Path] = []

            def canonicalize_parent(path, *, strict=False):
                resolved.append(path)
                if path == lexical_parent:
                    return canonical_parent
                if path == canonical_parent:
                    return canonical_parent
                raise AssertionError(f"unexpected path resolution: {path}")

            with mock.patch.object(Path, "resolve", new=canonicalize_parent):
                store.add_legacy_alias(
                    lexical_source,
                    "projects/p-one",
                    "migration-one",
                )
                alias = store.resolve_legacy_alias(canonical_source)

            self.assertEqual(
                alias,
                {
                    "target": "projects/p-one",
                    "migration_id": "migration-one",
                },
            )
            self.assertEqual(resolved, [lexical_parent, canonical_parent])
            self.assertNotIn(lexical_source, resolved)
            self.assertNotIn(canonical_source, resolved)

    def test_legacy_alias_rejects_product_paths_without_filesystem_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            home = temp / "home"
            store = self.registry_store_class()(temp / "loop")
            store.initialize()
            candidates = (
                home / ".codex" / "memories",
                home / ".codex" / "memories" / "archive" / "entry.json",
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
                    with self.subTest(candidate=candidate, operation="resolve"):
                        with self.assertRaises(LoopMemoryError) as context:
                            store.resolve_legacy_alias(candidate)
                        self.assertEqual(
                            context.exception.code,
                            "reserved_product_memory",
                        )
                    with self.subTest(candidate=candidate, operation="add"):
                        with self.assertRaises(LoopMemoryError) as context:
                            store.add_legacy_alias(
                                candidate,
                                "projects/p-one",
                                "migration-one",
                            )
                        self.assertEqual(
                            context.exception.code,
                            "reserved_product_memory",
                        )

    def test_corrupt_json_malformed_root_and_future_schema_fail_closed(self):
        malformed = {
            "schema_version": 1,
            "projects": [],
            "sessions": {},
            "legacy_aliases": {},
            "maintenance": {},
        }
        future = dict(INITIAL_STATE, schema_version=2)
        cases = (
            ("corrupt-json", b"not json\n", "corrupt_state"),
            (
                "malformed-version-one",
                json.dumps(malformed, sort_keys=True).encode("utf-8") + b"\n",
                "corrupt_state",
            ),
            (
                "future-schema",
                json.dumps(future, sort_keys=True).encode("utf-8") + b"\n",
                "unsupported_schema",
            ),
        )

        for name, original, error_code in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                loop_root = Path(temp_dir) / "loop"
                store = self.registry_store_class()(loop_root)
                store.initialize()
                (loop_root / "registry.json").write_bytes(original)

                with self.assertRaises(LoopMemoryError) as context:
                    store.initialize()

                self.assertEqual(context.exception.code, error_code)
                self.assertEqual((loop_root / "registry.json").read_bytes(), original)

    def test_malformed_version_one_records_fail_closed_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            valid_root = str((temp / "project").resolve(strict=False))
            valid_project = {
                "kind": "directory",
                "shared_roots": [],
                "roots": [valid_root],
                "upstream_refs": [],
            }

            def state_with(**updates):
                state = dict(INITIAL_STATE)
                state.update(updates)
                return state

            cases = (
                (
                    "invalid-project-id",
                    state_with(projects={"project": valid_project}),
                ),
                (
                    "duplicate-path-alias",
                    state_with(
                        projects={
                            "p-project": dict(
                                valid_project,
                                roots=[valid_root, valid_root],
                            )
                        }
                    ),
                ),
                (
                    "dangling-session-project",
                    state_with(
                        projects={"p-project": valid_project},
                        sessions={
                            "s-session": {
                                "project_id": "p-missing",
                                "thread_id": "thread",
                            }
                        },
                    ),
                ),
                (
                    "unnormalized-legacy-key",
                    state_with(
                        legacy_aliases={
                            "relative/legacy": {
                                "target": "projects/p-project",
                                "migration_id": "migration-one",
                            }
                        }
                    ),
                ),
                ("invalid-maintenance", state_with(maintenance=[])),
                ("unknown-root-field", dict(INITIAL_STATE, extra={})),
            )

            for name, state in cases:
                with self.subTest(name=name):
                    loop_root = temp / name
                    store = self.registry_store_class()(loop_root)
                    store.initialize()
                    original = self.write_state(loop_root, state)

                    with self.assertRaises(LoopMemoryError) as context:
                        store.resolve_legacy_alias(temp / "absent")

                    self.assertEqual(context.exception.code, "corrupt_state")
                    self.assertEqual(
                        (loop_root / "registry.json").read_bytes(),
                        original,
                    )

    def test_default_ids_and_injected_collision_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            default_store = self.registry_store_class()(temp / "default-loop")
            default_store.initialize()
            project_id = default_store.resolve_project(
                self.discovery(kind="directory", root=temp / "default-project"),
                create=True,
            )
            session_id = default_store.resolve_session(
                project_id,
                "thread",
                create=True,
            )
            self.assertRegex(project_id, r"^p-[0-9a-f]{32}$")
            self.assertRegex(session_id, r"^s-[0-9a-f]{32}$")

            collision_store = self.registry_store_class()(
                temp / "collision-loop",
                id_factory=lambda: "same",
            )
            collision_store.initialize()
            collision_store.resolve_project(
                self.discovery(kind="directory", root=temp / "one"),
                create=True,
            )
            original = (temp / "collision-loop" / "registry.json").read_bytes()

            with self.assertRaises(LoopMemoryError) as context:
                collision_store.resolve_project(
                    self.discovery(kind="directory", root=temp / "two"),
                    create=True,
                )

            self.assertEqual(context.exception.code, "id_collision")
            self.assertEqual(
                (temp / "collision-loop" / "registry.json").read_bytes(),
                original,
            )

    def test_mutations_use_registry_lease_without_residual_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            module = importlib.import_module("scripts.loopmem.registry")
            real_lease = module.FileLease
            lease_paths: list[Path] = []

            class ObservedLease(real_lease):
                def __init__(self, path, *args, **kwargs):
                    lease_paths.append(Path(path))
                    super().__init__(path, *args, **kwargs)

            with mock.patch.object(module, "FileLease", ObservedLease):
                store = module.RegistryStore(loop_root, id_factory=lambda: "one")
                store.initialize()
                project_id = store.resolve_project(
                    self.discovery(kind="directory", root=temp / "project"),
                    create=True,
                )
                store.resolve_session(project_id, "thread", create=True)
                store.add_legacy_alias(
                    temp / "legacy",
                    "projects/p-one",
                    "migration-one",
                )

            lease_path = (
                loop_root / "locks" / "registry.lock"
            ).resolve(strict=False)
            self.assertEqual(lease_paths, [lease_path] * 4)
            self.assertFalse(lease_path.exists())
            self.assertEqual(list(loop_root.rglob("*.tmp")), [])

    def test_live_registry_lease_fails_mutation_until_caller_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            suffixes = iter(("one", "two"))
            module = importlib.import_module("scripts.loopmem.registry")
            store = module.RegistryStore(
                loop_root,
                id_factory=lambda: next(suffixes),
            )
            store.initialize()
            store.resolve_project(
                self.discovery(kind="directory", root=temp / "first"),
                create=True,
            )
            original = store.registry_path.read_bytes()
            real_lease = module.FileLease
            holder = real_lease(store.lease_path, "holder")
            acquisition_attempts = 0

            class SingleAttemptLease(real_lease):
                def __enter__(self):
                    nonlocal acquisition_attempts
                    acquisition_attempts += 1
                    if acquisition_attempts > 1:
                        raise AssertionError("mutation retried a live lease conflict")
                    return super().__enter__()

            holder.__enter__()
            try:
                with mock.patch.object(module, "FileLease", SingleAttemptLease):
                    with self.assertRaises(LoopMemoryError) as context:
                        store.resolve_project(
                            self.discovery(kind="directory", root=temp / "second"),
                            create=True,
                        )

                    self.assertEqual(context.exception.code, "lease_busy")
                    self.assertEqual(acquisition_attempts, 1)
                    self.assertEqual(store.registry_path.read_bytes(), original)
            finally:
                holder.__exit__(None, None, None)

            project_id = store.resolve_project(
                self.discovery(kind="directory", root=temp / "second"),
                create=True,
            )
            self.assertEqual(project_id, "p-two")

    def test_concurrent_stores_serialize_real_project_mutations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop"
            module = importlib.import_module("scripts.loopmem.registry")
            first_store = module.RegistryStore(loop_root, id_factory=lambda: "one")
            second_store = module.RegistryStore(loop_root, id_factory=lambda: "two")
            first_store.initialize()
            real_lease = module.FileLease
            first_acquisition = threading.Barrier(2)
            winner_acquired = threading.Event()
            winner_released = threading.Event()
            errors: list[BaseException] = []

            class InterleavedLease(real_lease):
                def __enter__(self):
                    first_acquisition.wait(timeout=2)
                    if threading.current_thread().name == "winner":
                        result = super().__enter__()
                        winner_acquired.set()
                        return result

                    winner_acquired.wait(timeout=2)
                    try:
                        super().__enter__()
                    except LoopMemoryError as error:
                        if error.code != "lease_busy":
                            raise
                    else:
                        raise AssertionError("losing lease attempt unexpectedly succeeded")
                    winner_released.wait(timeout=2)
                    return super().__enter__()

                def __exit__(self, exc_type, exc_value, traceback):
                    try:
                        return super().__exit__(exc_type, exc_value, traceback)
                    finally:
                        if threading.current_thread().name == "winner":
                            winner_released.set()

            def create_project(store, root: Path) -> None:
                try:
                    store.resolve_project(
                        self.discovery(kind="directory", root=root),
                        create=True,
                    )
                except BaseException as error:
                    errors.append(error)

            first_thread = threading.Thread(
                target=create_project,
                args=(first_store, temp / "first"),
                name="winner",
                daemon=True,
            )
            second_thread = threading.Thread(
                target=create_project,
                args=(second_store, temp / "second"),
                name="loser",
                daemon=True,
            )
            with mock.patch.object(module, "FileLease", InterleavedLease):
                first_thread.start()
                second_thread.start()
                first_thread.join(timeout=3)
                second_thread.join(timeout=3)

            self.assertFalse(first_thread.is_alive(), "first mutation hung")
            self.assertFalse(second_thread.is_alive(), "second mutation hung")
            self.assertEqual(errors, [])
            state = json.loads(
                (loop_root / "registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(state["projects"]), {"p-one", "p-two"})
            self.assertFalse((loop_root / "locks" / "registry.lock").exists())
            self.assertEqual(list(loop_root.rglob("*.tmp")), [])

    def test_global_duplicate_strong_project_aliases_are_corrupt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            shared_common = str((temp / "shared.git").resolve(strict=False))
            shared_root = str((temp / "shared-root").resolve(strict=False))
            cases = (
                (
                    "common-dir",
                    {
                        "p-one": {
                            "kind": "git",
                            "shared_roots": [shared_common],
                            "roots": [str((temp / "one").resolve(strict=False))],
                            "upstream_refs": ["https://example.test/one.git"],
                        },
                        "p-two": {
                            "kind": "git",
                            "shared_roots": [shared_common],
                            "roots": [str((temp / "two").resolve(strict=False))],
                            "upstream_refs": ["https://example.test/two.git"],
                        },
                    },
                ),
                (
                    "root",
                    {
                        "p-one": {
                            "kind": "directory",
                            "shared_roots": [],
                            "roots": [shared_root],
                            "upstream_refs": [],
                        },
                        "p-two": {
                            "kind": "directory",
                            "shared_roots": [],
                            "roots": [shared_root],
                            "upstream_refs": [],
                        },
                    },
                ),
            )

            for name, projects in cases:
                with self.subTest(name=name):
                    loop_root = temp / name
                    store = self.registry_store_class()(loop_root)
                    store.initialize()
                    state = dict(INITIAL_STATE)
                    state["projects"] = projects
                    original = self.write_state(loop_root, state)

                    with self.assertRaises(LoopMemoryError) as context:
                        store.resolve_legacy_alias(temp / "unrelated")

                    self.assertEqual(context.exception.code, "corrupt_state")
                    self.assertEqual(
                        (loop_root / "registry.json").read_bytes(),
                        original,
                    )

    def test_global_duplicate_restorable_sessions_are_corrupt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = {
                "kind": "directory",
                "shared_roots": [],
                "roots": [str((temp / "project").resolve(strict=False))],
                "upstream_refs": [],
            }
            sessions = {
                "s-one": {"project_id": "p-project", "thread_id": "thread"},
                "s-two": {"project_id": "p-project", "thread_id": "thread"},
            }
            loop_root = temp / "duplicate-restorable"
            store = self.registry_store_class()(loop_root)
            store.initialize()
            state = dict(INITIAL_STATE)
            state["projects"] = {"p-project": project}
            state["sessions"] = sessions
            original = self.write_state(loop_root, state)

            with self.assertRaises(LoopMemoryError) as context:
                store.resolve_legacy_alias(temp / "unrelated")

            self.assertEqual(context.exception.code, "ambiguous_session")
            self.assertEqual((loop_root / "registry.json").read_bytes(), original)

            local_root = temp / "duplicate-local"
            local_store = self.registry_store_class()(local_root)
            local_store.initialize()
            local_state = dict(INITIAL_STATE)
            local_state["projects"] = {"p-project": project}
            local_state["sessions"] = {
                "s-one": {"project_id": "p-project", "thread_id": None},
                "s-two": {"project_id": "p-project", "thread_id": None},
            }
            self.write_state(local_root, local_state)

            self.assertIsNone(
                local_store.resolve_legacy_alias(temp / "unrelated-local")
            )


if __name__ == "__main__":
    unittest.main()
