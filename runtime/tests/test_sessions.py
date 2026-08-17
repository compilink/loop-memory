import errno
import fcntl
import importlib
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError


PROJECT_TEMPLATE = (
    "# Project Memory\n"
    "\n"
    "## Verified Facts\n"
    "\n"
    "## Engineering Patterns\n"
    "\n"
    "## Decisions\n"
    "\n"
    "## Risks\n"
    "\n"
    "## Superseded\n"
)
VERIFIED_ENTRY = (
    "- [2026-08-10][verified] Worktrees share project engineering knowledge.\n"
    "  Evidence: git common-dir identity test\n"
)
INFERRED_ENTRY = (
    "- [2026-08-10][inferred] A repeated observation may become durable.\n"
    "  Evidence: two independent trial runs\n"
)


class SessionMemoryTests(unittest.TestCase):
    def sessions_module(self):
        try:
            return importlib.import_module("scripts.loopmem.sessions")
        except ModuleNotFoundError:
            self.fail("scripts.loopmem.sessions has not been implemented")

    def assert_loop_error(self, code: str, operation) -> LoopMemoryError:
        with self.assertRaises(LoopMemoryError) as context:
            operation()
        self.assertEqual(context.exception.code, code)
        return context.exception

    def test_full_initialization_creates_canonical_layout_only_under_root(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            loop_root = temp / "loop-memory"
            project_root = temp / "worktree"
            project_root.mkdir()

            module.ensure_global_layout(loop_root)
            project_dir = module.ensure_project_layout(loop_root, "p-project")
            session_dir = module.ensure_session_layout(
                loop_root,
                "p-project",
                "s-session",
            )

            expected_files = {
                Path("global/long.md"),
                Path("global/medium.md"),
                Path("global/short.md"),
                Path("global/facts/index.md"),
                Path("projects/p-project/project.md"),
                Path("projects/p-project/sessions/active/s-session/status.md"),
                Path("projects/p-project/sessions/active/s-session/handoff.md"),
                Path(
                    "projects/p-project/sessions/active/s-session/agents/main/inbox.md"
                ),
                Path(
                    "projects/p-project/sessions/active/s-session/agents/main/outbox.md"
                ),
            }
            actual_files = {
                path.relative_to(loop_root)
                for path in loop_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual_files, expected_files)
            self.assertEqual(project_dir, (loop_root / "projects/p-project").resolve())
            self.assertEqual(
                session_dir,
                (
                    loop_root
                    / "projects/p-project/sessions/active/s-session"
                ).resolve(),
            )
            self.assertEqual(
                (project_dir / "project.md").read_text(encoding="utf-8"),
                PROJECT_TEMPLATE,
            )
            long_content = (loop_root / "global/long.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                long_content,
                "# Global Long-Term Memory\n\n"
                "## Methodology\n\n"
                "## Fact Index\n\n"
                "- `~/loop-memory/global/facts/index.md`\n",
            )
            for name, heading in (
                ("medium.md", "# Global Medium-Term Memory"),
                ("short.md", "# Global Short-Term Memory"),
            ):
                content = (loop_root / "global" / name).read_text(encoding="utf-8")
                self.assertTrue(content.startswith(f"{heading}\n"))
                self.assertIn("\n## Entries\n", content)

            for directory in (loop_root, *[p for p in loop_root.rglob("*") if p.is_dir()]):
                self.assertTrue(directory.is_dir())
            for path in (p for p in loop_root.rglob("*") if p.is_file()):
                self.assertTrue(path.is_file())
            self.assertFalse((project_root / ".memory").exists())
            self.assertFalse(any(path.is_file() for path in project_root.rglob("*")))

    def test_initialization_is_additive_and_preserves_existing_text(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            session_dir = module.ensure_session_layout(loop_root, "p-one", "s-one")
            preserved = {
                loop_root / "global/long.md": "custom global text\n",
                loop_root / "projects/p-one/project.md": "custom project text\n",
                session_dir / "status.md": "custom status text\n",
                session_dir / "agents/main/inbox.md": "custom inbox text\n",
            }
            for path, content in preserved.items():
                path.write_text(content, encoding="utf-8")

            module.ensure_global_layout(loop_root)
            module.ensure_project_layout(loop_root, "p-one")
            module.ensure_session_layout(loop_root, "p-one", "s-one")

            for path, content in preserved.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_additive_file_creation_never_replaces_a_concurrent_winner(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "layout"
            parent.mkdir()
            target = parent / "status.md"
            winner = b"concurrent winner\n"
            real_open = Path.open
            injected = False

            def racing_open(path, mode="r", *args, **kwargs):
                nonlocal injected
                if Path(path) == target and mode == "xb" and not injected:
                    target.write_bytes(winner)
                    injected = True
                return real_open(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", new=racing_open):
                module._ensure_file(target, "# Session Status\n")

            self.assertTrue(injected)
            self.assertEqual(target.read_bytes(), winner)

    def test_failed_template_create_preserves_cooperative_foreign_replacement(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "layout"
            parent.mkdir()
            target = parent / "status.md"
            foreign = "cooperative foreign replacement\n"
            parent_stat = parent.stat()
            parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
            cleanup_boundary = threading.Event()
            writer_lock_attempted = threading.Event()
            writer_lock_acquired = threading.Event()
            writer_done = threading.Event()
            writer_errors = []
            real_fsync_directory = module._fsync_directory
            real_flock = fcntl.flock
            real_unlink = Path.unlink
            failure_injected = False
            cleanup_observed = False

            def fail_first_parent_fsync(path: Path) -> None:
                nonlocal failure_injected
                if path == parent and not failure_injected:
                    failure_injected = True
                    raise OSError("injected template parent fsync failure")
                real_fsync_directory(path)

            def observed_flock(descriptor: int, operation: int):
                descriptor_stat = os.fstat(descriptor)
                identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
                if (
                    threading.current_thread() is writer
                    and operation == fcntl.LOCK_EX
                    and identity == parent_identity
                ):
                    writer_lock_attempted.set()
                    result = real_flock(descriptor, operation)
                    writer_lock_acquired.set()
                    return result
                return real_flock(descriptor, operation)

            def observed_unlink(path: Path, *args, **kwargs):
                nonlocal cleanup_observed
                if path == target and not cleanup_observed:
                    cleanup_observed = True
                    cleanup_boundary.set()
                    if not writer_lock_attempted.wait(2):
                        raise AssertionError("cooperative writer did not attempt parent lock")
                    if writer_lock_acquired.wait(0.1):
                        if not writer_done.wait(2):
                            raise AssertionError("unlocked cooperative writer did not finish")
                return real_unlink(path, *args, **kwargs)

            def cooperative_writer() -> None:
                try:
                    if not cleanup_boundary.wait(2):
                        raise AssertionError("template cleanup boundary was not reached")
                    module.write_text_atomic(target, foreign)
                except BaseException as error:
                    writer_errors.append(error)
                finally:
                    writer_done.set()

            writer = threading.Thread(target=cooperative_writer)
            with (
                mock.patch.object(
                    module,
                    "_fsync_directory",
                    new=fail_first_parent_fsync,
                ),
                mock.patch.object(fcntl, "flock", new=observed_flock),
                mock.patch.object(Path, "unlink", new=observed_unlink),
            ):
                writer.start()
                with self.assertRaisesRegex(
                    OSError,
                    "injected template parent fsync failure",
                ):
                    module._ensure_file(target, "# Session Status\n")
                writer.join(2)

            self.assertFalse(writer.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertTrue(failure_injected)
            self.assertTrue(cleanup_observed)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), foreign)

            stale_stat = target.stat()
            stale_identity = (stale_stat.st_dev, stale_stat.st_ino)
            module.write_text_atomic(target, "newer cooperative replacement\n")
            removed = module._unlink_if_identity(target, stale_identity)
            self.assertFalse(removed)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "newer cooperative replacement\n",
            )

    def test_fstat_failure_cleans_exclusive_template_file_and_descriptor(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "layout"
            parent.mkdir()
            target = parent / "status.md"
            opened_streams = []
            real_open = Path.open
            real_fstat = os.fstat

            def observed_open(path, mode="r", *args, **kwargs):
                stream = real_open(path, mode, *args, **kwargs)
                if Path(path) == target and mode == "xb":
                    opened_streams.append(stream)
                return stream

            def failing_fstat(descriptor: int):
                if any(descriptor == stream.fileno() for stream in opened_streams):
                    raise OSError("injected template fstat failure")
                return real_fstat(descriptor)

            with (
                mock.patch.object(Path, "open", new=observed_open),
                mock.patch.object(module.os, "fstat", new=failing_fstat),
            ):
                with self.assertRaisesRegex(OSError, "injected template fstat failure"):
                    module._ensure_file(target, "# Session Status\n")

            self.assertEqual(len(opened_streams), 1)
            self.assertTrue(opened_streams[0].closed)
            self.assertFalse(target.exists())

            module._ensure_file(target, "# Session Status\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "# Session Status\n")

    def test_fstat_failure_does_not_delete_foreign_template_replacement(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "layout"
            parent.mkdir()
            target = parent / "status.md"
            foreign = b"foreign replacement\n"
            real_fstat = os.fstat
            injected = False

            def replace_then_fail(descriptor: int):
                nonlocal injected
                if not injected and stat.S_ISREG(real_fstat(descriptor).st_mode):
                    injected = True
                    target.unlink()
                    target.write_bytes(foreign)
                    raise OSError("injected template fstat replacement failure")
                return real_fstat(descriptor)

            with mock.patch.object(
                module.os,
                "fstat",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected template fstat replacement failure",
                ):
                    module._ensure_file(target, "# Session Status\n")

            self.assertEqual(target.read_bytes(), foreign)

    def test_session_writes_target_main_and_only_the_requested_subagent(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            expected = {
                ("status", None): "status body\n",
                ("handoff", None): "handoff body\n",
                ("inbox", None): "main inbox\n",
                ("outbox", None): "main outbox\n",
                ("inbox", "worker.one"): "worker inbox\n",
                ("outbox", "worker.one"): "worker outbox\n",
                ("outbox", "worker-two"): "other worker outbox\n",
            }

            destinations = {}
            for (kind, agent_id), content in expected.items():
                destination = module.write_session_file(
                    loop_root,
                    "p-project",
                    "s-session",
                    kind,
                    content,
                    agent_id,
                )
                destinations[(kind, agent_id)] = destination
                self.assertEqual(destination.read_text(encoding="utf-8"), content)
                self.assertEqual(destination, destination.resolve())

            session = (
                loop_root / "projects/p-project/sessions/active/s-session"
            ).resolve()
            self.assertEqual(destinations[("status", None)], session / "status.md")
            self.assertEqual(destinations[("handoff", None)], session / "handoff.md")
            self.assertEqual(
                destinations[("inbox", None)],
                session / "agents/main/inbox.md",
            )
            self.assertEqual(
                destinations[("outbox", "worker.one")],
                session / "agents/subagents/worker.one/outbox.md",
            )
            other_before = (session / "agents/subagents/worker-two/outbox.md").read_bytes()
            project_before = (loop_root / "projects/p-project/project.md").read_bytes()
            global_before = (loop_root / "global/long.md").read_bytes()

            module.write_session_file(
                loop_root,
                "p-project",
                "s-session",
                "inbox",
                "worker inbox updated\n",
                "worker.one",
            )

            self.assertEqual(
                (session / "agents/subagents/worker-two/outbox.md").read_bytes(),
                other_before,
            )
            self.assertEqual(
                (loop_root / "projects/p-project/project.md").read_bytes(),
                project_before,
            )
            self.assertEqual((loop_root / "global/long.md").read_bytes(), global_before)

    def test_subagent_symlink_to_main_is_rejected_without_modifying_main(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            session = module.ensure_session_layout(
                loop_root,
                "p-project",
                "s-session",
            )
            main = session / "agents/main"
            worker = session / "agents/subagents/worker"
            worker.symlink_to(main, target_is_directory=True)
            before = {
                kind: (main / f"{kind}.md").read_bytes()
                for kind in ("inbox", "outbox")
            }

            for kind in ("inbox", "outbox"):
                with self.subTest(kind=kind):
                    self.assert_loop_error(
                        "unsafe_path",
                        lambda kind=kind: module.write_session_file(
                            loop_root,
                            "p-project",
                            "s-session",
                            kind,
                            "redirected write\n",
                            "worker",
                        ),
                    )
                    self.assertEqual((main / f"{kind}.md").read_bytes(), before[kind])

    def test_invalid_ids_kinds_and_subagent_main_files_fail_before_writes(self):
        module = self.sessions_module()
        operations = (
            ("project traversal", lambda root: module.ensure_project_layout(root, "../p-x")),
            ("project prefix", lambda root: module.ensure_project_layout(root, "project")),
            (
                "session traversal",
                lambda root: module.ensure_session_layout(root, "p-one", "s/../../x"),
            ),
            (
                "session prefix",
                lambda root: module.ensure_session_layout(root, "p-one", "session"),
            ),
            (
                "agent traversal",
                lambda root: module.write_session_file(
                    root, "p-one", "s-one", "inbox", "x", "../worker"
                ),
            ),
            (
                "invalid kind",
                lambda root: module.write_session_file(
                    root, "p-one", "s-one", "notes", "x"
                ),
            ),
            (
                "subagent status",
                lambda root: module.write_session_file(
                    root, "p-one", "s-one", "status", "x", "worker"
                ),
            ),
            (
                "subagent handoff",
                lambda root: module.write_session_file(
                    root, "p-one", "s-one", "handoff", "x", "worker"
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for index, (name, operation) in enumerate(operations):
                with self.subTest(name=name):
                    loop_root = temp / str(index) / "loop"
                    with self.assertRaises(LoopMemoryError):
                        operation(loop_root)
                    self.assertFalse(loop_root.exists())

    def test_inferred_durable_promotions_are_rejected_before_lease_construction(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with mock.patch.object(
                module,
                "FileLease",
                side_effect=AssertionError("lease must not be constructed"),
            ):
                for scope, section in (
                    ("project", "Engineering Patterns"),
                    ("global-long", "Methodology"),
                ):
                    with self.subTest(scope=scope):
                        root = temp / scope
                        self.assert_loop_error(
                            "inferred_not_durable",
                            lambda root=root, scope=scope, section=section: module.promote_entry(
                                root,
                                "p-project",
                                scope,
                                section,
                                INFERRED_ENTRY,
                            ),
                        )
                        self.assertFalse(root.exists())

    def test_verified_entry_is_inserted_in_project_section_without_touching_other_text(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            project_dir = module.ensure_project_layout(loop_root, "p-project")
            project_path = project_dir / "project.md"
            before = PROJECT_TEMPLATE.replace(
                "## Decisions\n",
                "Unrelated exact text with spaces  \n\n## Decisions\n",
            ).encode("utf-8")
            project_path.write_bytes(before)

            changed = module.promote_entry(
                loop_root,
                "p-project",
                "project",
                "Verified Facts",
                VERIFIED_ENTRY,
            )

            after = project_path.read_bytes()
            self.assertTrue(changed)
            self.assertIn(VERIFIED_ENTRY.encode("utf-8"), after)
            self.assertIn(b"Unrelated exact text with spaces  \n", after)
            self.assertLess(
                after.index(VERIFIED_ENTRY.encode("utf-8")),
                after.index(b"## Engineering Patterns"),
            )

    def test_inferred_entry_is_inserted_in_global_medium_entries(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"

            changed = module.promote_entry(
                loop_root,
                "p-project",
                "global-medium",
                "Entries",
                INFERRED_ENTRY,
            )

            self.assertTrue(changed)
            medium = (loop_root / "global/medium.md").read_text(encoding="utf-8")
            self.assertIn(INFERRED_ENTRY, medium)

    def test_exact_normalized_duplicate_does_not_rewrite_destination(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            unnormalized = (
                "\r\n- [2026-08-10][verified] Worktrees share project engineering knowledge.  \r\n"
                "  Evidence: git common-dir identity test\t\r\n\r\n"
            )

            self.assertTrue(
                module.promote_entry(
                    loop_root,
                    "p-project",
                    "project",
                    "Verified Facts",
                    unnormalized,
                )
            )
            project_path = loop_root / "projects/p-project/project.md"
            before = project_path.read_bytes()

            changed = module.promote_entry(
                loop_root,
                "p-project",
                "project",
                "Verified Facts",
                "\n" + VERIFIED_ENTRY + "\n",
            )

            self.assertFalse(changed)
            self.assertEqual(project_path.read_bytes(), before)
            self.assertEqual(before.count(VERIFIED_ENTRY.encode("utf-8")), 1)

    def test_adjacent_exact_duplicate_is_detected_without_rewrite(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            project_path = module.ensure_project_layout(
                loop_root,
                "p-project",
            ) / "project.md"
            previous = (
                "- [2026-08-09][verified] Earlier verified project fact.\n"
                "  Evidence: prior source\n"
            )
            content = PROJECT_TEMPLATE.replace(
                "## Verified Facts\n\n## Engineering Patterns",
                (
                    "## Verified Facts\n\n"
                    + previous
                    + VERIFIED_ENTRY
                    + "\n## Engineering Patterns"
                ),
            )
            project_path.write_text(content, encoding="utf-8")
            before = project_path.read_bytes()

            changed = module.promote_entry(
                loop_root,
                "p-project",
                "project",
                "Verified Facts",
                VERIFIED_ENTRY,
            )

            self.assertFalse(changed)
            self.assertEqual(project_path.read_bytes(), before)

    def test_duplicate_before_adjacent_legacy_bullet_is_detected_without_rewrite(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            project_path = module.ensure_project_layout(
                loop_root,
                "p-project",
            ) / "project.md"
            legacy = "- 2026-08-11: migrated legacy fact\n"
            content = PROJECT_TEMPLATE.replace(
                "## Verified Facts\n\n## Engineering Patterns",
                (
                    "## Verified Facts\n\n"
                    + VERIFIED_ENTRY
                    + legacy
                    + "\n## Engineering Patterns"
                ),
            )
            project_path.write_text(content, encoding="utf-8")
            before = project_path.read_bytes()

            changed = module.promote_entry(
                loop_root,
                "p-project",
                "project",
                "Verified Facts",
                VERIFIED_ENTRY,
            )

            self.assertFalse(changed)
            self.assertEqual(project_path.read_bytes(), before)

    def test_bad_headings_status_section_and_entry_fail_closed(self):
        module = self.sessions_module()
        malformed_cases = (
            (
                "missing heading",
                "# Project Memory\n\n## Decisions\n",
                "Verified Facts",
                VERIFIED_ENTRY,
                "invalid_section_heading",
            ),
            (
                "duplicate heading",
                PROJECT_TEMPLATE + "\n## Verified Facts\n",
                "Verified Facts",
                VERIFIED_ENTRY,
                "invalid_section_heading",
            ),
            (
                "bad status",
                PROJECT_TEMPLATE,
                "Verified Facts",
                "- [2026-08-10][unknown] Claim\n  Evidence: source\n",
                "invalid_entry",
            ),
            (
                "bad section",
                PROJECT_TEMPLATE,
                "Unknown",
                VERIFIED_ENTRY,
                "invalid_section",
            ),
            (
                "missing evidence",
                PROJECT_TEMPLATE,
                "Verified Facts",
                "- [2026-08-10][verified] Claim\n",
                "invalid_entry",
            ),
            (
                "heading injection",
                PROJECT_TEMPLATE,
                "Verified Facts",
                "- [2026-08-10][verified] Claim\n## Injected\n  Evidence: source\n",
                "invalid_entry",
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for index, (name, content, section, entry, code) in enumerate(malformed_cases):
                with self.subTest(name=name):
                    loop_root = temp / str(index) / "loop"
                    project_path = module.ensure_project_layout(
                        loop_root, "p-project"
                    ) / "project.md"
                    project_path.write_text(content, encoding="utf-8")
                    before = project_path.read_bytes()

                    self.assert_loop_error(
                        code,
                        lambda: module.promote_entry(
                            loop_root,
                            "p-project",
                            "project",
                            section,
                            entry,
                        ),
                    )
                    self.assertEqual(project_path.read_bytes(), before)

    def test_live_promotion_lease_conflict_surfaces_busy_without_rewrite(self):
        module = self.sessions_module()
        real_lease = module.FileLease

        class LiveConflictLease:
            def __init__(self, path: Path, owner: str) -> None:
                self.path = path

            def __enter__(self):
                with real_lease(self.path, "live-holder"):
                    return real_lease(self.path, "contender").__enter__()

            def __exit__(self, exc_type, exc_value, traceback):
                raise AssertionError("conflicting lease unexpectedly acquired")

        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            project_path = module.ensure_project_layout(
                loop_root, "p-project"
            ) / "project.md"
            before = project_path.read_bytes()

            with mock.patch.object(module, "FileLease", LiveConflictLease):
                self.assert_loop_error(
                    "lease_busy",
                    lambda: module.promote_entry(
                        loop_root,
                        "p-project",
                        "project",
                        "Verified Facts",
                        VERIFIED_ENTRY,
                    ),
                )

            self.assertEqual(project_path.read_bytes(), before)
            self.assertEqual(list((loop_root / "locks").iterdir()), [])

    def test_live_promotion_conflict_does_not_initialize_memory_layout(self):
        module = self.sessions_module()
        real_lease = module.FileLease

        class LiveConflictLease:
            def __init__(self, path: Path, owner: str) -> None:
                self.path = path

            def __enter__(self):
                with real_lease(self.path, "live-holder"):
                    return real_lease(self.path, "contender").__enter__()

            def __exit__(self, exc_type, exc_value, traceback):
                raise AssertionError("conflicting lease unexpectedly acquired")

        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"

            with mock.patch.object(module, "FileLease", LiveConflictLease):
                self.assert_loop_error(
                    "lease_busy",
                    lambda: module.promote_entry(
                        loop_root,
                        "p-project",
                        "project",
                        "Verified Facts",
                        VERIFIED_ENTRY,
                    ),
                )

            self.assertTrue((loop_root / "locks").is_dir())
            self.assertEqual(list((loop_root / "locks").iterdir()), [])
            self.assertFalse((loop_root / "global").exists())
            self.assertFalse((loop_root / "projects").exists())

    def test_archive_moves_full_tree_to_deterministic_month_and_is_idempotent(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            module.write_session_file(
                loop_root,
                "p-project",
                "s-session",
                "outbox",
                "delegated result\n",
                "worker",
            )
            nested = source / "agents/subagents/worker/evidence.bin"
            nested.write_bytes(b"full tree evidence\x00")
            now = datetime(2026, 8, 10, 15, 30)

            destination = module.archive_session(
                loop_root,
                "p-project",
                "s-session",
                now,
            )

            expected = (
                loop_root
                / "projects/p-project/sessions/archive/2026-08/s-session"
            ).resolve()
            self.assertEqual(destination, expected)
            self.assertFalse(source.exists())
            self.assertEqual(
                (destination / "agents/subagents/worker/outbox.md").read_text(
                    encoding="utf-8"
                ),
                "delegated result\n",
            )
            self.assertEqual(
                (destination / "agents/subagents/worker/evidence.bin").read_bytes(),
                b"full tree evidence\x00",
            )
            self.assertEqual(
                module.archive_session(loop_root, "p-project", "s-session", now),
                expected,
            )

    def test_archive_sets_directory_mtime_to_explicit_archive_time(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            old = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            now = datetime(2026, 8, 10, 15, 30, tzinfo=timezone.utc)
            old_ns = int(old.timestamp()) * 1_000_000_000
            now_ns = int(now.timestamp()) * 1_000_000_000
            os.utime(source, ns=(old_ns, old_ns))
            self.assertEqual(source.stat().st_mtime_ns, old_ns)

            destination = module.archive_session(
                loop_root,
                "p-project",
                "s-session",
                now,
            )

            self.assertEqual(destination.stat().st_mtime_ns, now_ns)

    def test_archived_session_cannot_be_recreated_by_ensure_or_write(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            destination = module.archive_session(
                loop_root,
                "p-project",
                "s-session",
                datetime(2026, 8, 1),
            )
            archived_status = (destination / "status.md").read_bytes()

            self.assert_loop_error(
                "session_archived",
                lambda: module.ensure_session_layout(
                    loop_root,
                    "p-project",
                    "s-session",
                ),
            )
            self.assert_loop_error(
                "session_archived",
                lambda: module.write_session_file(
                    loop_root,
                    "p-project",
                    "s-session",
                    "status",
                    "resurrected\n",
                ),
            )
            self.assertFalse(source.exists())
            self.assertEqual((destination / "status.md").read_bytes(), archived_status)

            duplicate = (
                loop_root
                / "projects/p-project/sessions/archive/2026-09/s-session"
            )
            duplicate.mkdir(parents=True)
            self.assert_loop_error(
                "corrupt_state",
                lambda: module.ensure_session_layout(
                    loop_root,
                    "p-project",
                    "s-session",
                ),
            )

    def test_archive_keeps_previous_generation_tree_immutable(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            active = module.ensure_session_layout(loop_root, "p-project", "s-one")
            module.write_session_file(loop_root, "p-project", "s-one", "handoff", "old handoff\n")
            archived = module.archive_session(loop_root, "p-project", "s-one", datetime(2026, 8, 1))
            before = (archived / "handoff.md").read_bytes()
            self.assertFalse(archived.is_symlink())
            self.assertFalse(active.exists())
            self.assertEqual((archived / "handoff.md").read_bytes(), before)

    def test_archive_waits_for_inflight_write_and_includes_completed_bytes(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            sessions_dir = source.parent.parent
            sessions_stat = sessions_dir.stat()
            sessions_identity = (sessions_stat.st_dev, sessions_stat.st_ino)
            write_entered = threading.Event()
            release_write = threading.Event()
            archive_lock_attempted = threading.Event()
            archive_done = threading.Event()
            errors = []
            result = {}
            real_write = module.write_text_atomic
            real_flock = fcntl.flock

            def blocking_write(path: Path, value: str) -> None:
                if path.name == "status.md" and value == "completed write\n":
                    write_entered.set()
                    if not release_write.wait(2):
                        raise AssertionError("write release was not signaled")
                real_write(path, value)

            def observed_flock(descriptor: int, operation: int):
                descriptor_stat = os.fstat(descriptor)
                identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
                if operation == fcntl.LOCK_EX and identity == sessions_identity:
                    archive_lock_attempted.set()
                return real_flock(descriptor, operation)

            def write_worker() -> None:
                try:
                    module.write_session_file(
                        loop_root,
                        "p-project",
                        "s-session",
                        "status",
                        "completed write\n",
                    )
                except BaseException as error:
                    errors.append(error)

            def archive_worker() -> None:
                try:
                    result["destination"] = module.archive_session(
                        loop_root,
                        "p-project",
                        "s-session",
                        datetime(2026, 8, 1),
                    )
                except BaseException as error:
                    errors.append(error)
                finally:
                    archive_done.set()

            writer = threading.Thread(target=write_worker)
            archiver = threading.Thread(target=archive_worker)
            with (
                mock.patch.object(module, "write_text_atomic", new=blocking_write),
                mock.patch.object(fcntl, "flock", new=observed_flock),
            ):
                writer.start()
                self.assertTrue(write_entered.wait(2))
                archiver.start()
                try:
                    self.assertTrue(archive_lock_attempted.wait(2))
                    self.assertFalse(archive_done.is_set())
                finally:
                    release_write.set()
                writer.join(2)
                archiver.join(2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(archiver.is_alive())
            self.assertEqual(errors, [])
            destination = result["destination"]
            self.assertFalse(source.exists())
            self.assertEqual(
                (destination / "status.md").read_text(encoding="utf-8"),
                "completed write\n",
            )

    def test_archive_destination_conflict_preserves_active_and_archive_trees(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            (source / "status.md").write_text("active\n", encoding="utf-8")
            destination = (
                loop_root
                / "projects/p-project/sessions/archive/2026-08/s-session"
            )
            destination.mkdir(parents=True)
            (destination / "status.md").write_text("archived\n", encoding="utf-8")

            self.assert_loop_error(
                "corrupt_state",
                lambda: module.archive_session(
                    loop_root,
                    "p-project",
                    "s-session",
                    datetime(2026, 8, 1),
                ),
            )

            self.assertEqual((source / "status.md").read_text(), "active\n")
            self.assertEqual((destination / "status.md").read_text(), "archived\n")

    def test_archive_rejects_regular_file_active_source_without_moving_it(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            project = module.ensure_project_layout(loop_root, "p-project")
            source = project / "sessions/active/s-session"
            source.write_text("not a session directory\n", encoding="utf-8")

            error = self.assert_loop_error(
                "corrupt_state",
                lambda: module.archive_session(
                    loop_root,
                    "p-project",
                    "s-session",
                    datetime(2026, 8, 1),
                ),
            )

            self.assertFalse(error.recoverable)
            self.assertEqual(source.read_text(), "not a session directory\n")
            self.assertFalse(
                (
                    project
                    / "sessions/archive/2026-08/s-session"
                ).exists()
            )

    def test_archive_rejects_regular_file_archived_destination(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            project = module.ensure_project_layout(loop_root, "p-project")
            destination = project / "sessions/archive/2026-08/s-session"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("not an archive directory\n", encoding="utf-8")

            error = self.assert_loop_error(
                "corrupt_state",
                lambda: module.archive_session(
                    loop_root,
                    "p-project",
                    "s-session",
                    datetime(2026, 8, 1),
                ),
            )

            self.assertFalse(error.recoverable)
            self.assertEqual(destination.read_text(), "not an archive directory\n")

    def test_archive_race_uses_atomic_no_replace_and_preserves_both_trees(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            (source / "status.md").write_text("active\n", encoding="utf-8")
            destination = (
                loop_root
                / "projects/p-project/sessions/archive/2026-08/s-session"
            ).resolve()
            legacy_rename = os.rename
            real_no_replace = getattr(module, "_rename_no_replace", legacy_rename)

            def create_competitor_then_rename(source_path: Path, destination_path: Path):
                destination.mkdir()
                (destination / "status.md").write_text(
                    "competing archive\n",
                    encoding="utf-8",
                )
                return real_no_replace(source_path, destination_path)

            with mock.patch.object(
                module,
                "_rename_no_replace",
                new=create_competitor_then_rename,
                create=True,
            ):
                self.assert_loop_error(
                    "archive_conflict",
                    lambda: module.archive_session(
                        loop_root,
                        "p-project",
                        "s-session",
                        datetime(2026, 8, 1),
                    ),
                )

            self.assertEqual((source / "status.md").read_text(), "active\n")
            self.assertEqual(
                (destination / "status.md").read_text(),
                "competing archive\n",
            )

    def test_archive_retry_reissues_parent_fsyncs_after_post_rename_failure(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            source = module.ensure_session_layout(loop_root, "p-project", "s-session")
            now = datetime(2026, 8, 1)
            active_parent = source.parent.resolve()
            archive_parent = (
                loop_root / "projects/p-project/sessions/archive/2026-08"
            ).resolve()
            archive_root = archive_parent.parent
            calls = []
            real_fsync = module._fsync_directory

            def fail_once(path: Path) -> None:
                calls.append(path)
                if len(calls) == 4:
                    raise OSError("injected directory fsync failure")
                real_fsync(path)

            with mock.patch.object(module, "_fsync_directory", new=fail_once):
                with self.assertRaisesRegex(OSError, "injected directory fsync failure"):
                    module.archive_session(
                        loop_root,
                        "p-project",
                        "s-session",
                        now,
                    )
                retry_error = None
                try:
                    destination = module.archive_session(
                        loop_root,
                        "p-project",
                        "s-session",
                        datetime(2026, 9, 1),
                    )
                except LoopMemoryError as error:
                    retry_error = error.code

            self.assertIsNone(retry_error, f"cross-month retry failed: {retry_error}")

            self.assertFalse(source.exists())
            self.assertEqual(destination, archive_parent / "s-session")
            self.assertEqual(
                calls,
                [
                    source,
                    active_parent,
                    archive_parent,
                    archive_root,
                    active_parent,
                    archive_parent,
                    archive_root,
                ],
            )

    def test_archive_missing_source_and_destination_is_typed_not_found(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"

            self.assert_loop_error(
                "session_not_found",
                lambda: module.archive_session(
                    loop_root,
                    "p-project",
                    "s-session",
                    datetime(2026, 8, 1),
                ),
            )

    def test_successful_operations_leave_no_lock_or_temp_residue(self):
        module = self.sessions_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            loop_root = Path(temp_dir) / "loop"
            module.ensure_session_layout(loop_root, "p-project", "s-session")
            module.write_session_file(
                loop_root,
                "p-project",
                "s-session",
                "status",
                "working\n",
            )
            module.promote_entry(
                loop_root,
                "p-project",
                "project",
                "Verified Facts",
                VERIFIED_ENTRY,
            )
            module.archive_session(
                loop_root,
                "p-project",
                "s-session",
                datetime(2026, 8, 1),
            )

            residue = [
                path
                for path in loop_root.rglob("*")
                if path.name.endswith((".lock", ".tmp"))
            ]
            self.assertEqual(residue, [])
            self.assertEqual(list((loop_root / "locks").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
