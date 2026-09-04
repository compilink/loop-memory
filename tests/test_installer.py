import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import install


class ManagedBlockTests(unittest.TestCase):
    def test_exact_unmarked_payload_converges_to_one_managed_block(self):
        payload = "# AGENTS.md\n\nMethod.\n"
        merged = install.merge_managed_block(payload, payload)
        self.assertEqual(merged, install._render_block(payload) + "\n")

    def test_append_preserves_existing_bytes_as_prefix(self):
        existing = "# Personal rules\n\nKeep this."
        merged = install.merge_managed_block(existing, "## Loop Engineering\n\nMethod.")
        self.assertTrue(merged.startswith(existing))
        self.assertEqual(merged.count(install.BEGIN_MARKER), 1)
        self.assertEqual(merged.count(install.END_MARKER), 1)

    def test_replace_preserves_bytes_around_exact_block(self):
        existing = (
            "before\n"
            + install.BEGIN_MARKER + "\nold\n" + install.END_MARKER
            + "\nafter\n"
        )
        merged = install.merge_managed_block(existing, "new")
        self.assertEqual(
            merged,
            "before\n" + install.BEGIN_MARKER + "\nnew\n"
            + install.END_MARKER + "\nafter\n",
        )

    def test_partial_duplicate_or_reversed_markers_are_rejected(self):
        cases = (
            install.BEGIN_MARKER,
            install.END_MARKER,
            install.BEGIN_MARKER + install.BEGIN_MARKER + install.END_MARKER,
            install.END_MARKER + install.BEGIN_MARKER,
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(install.InstallerError, "ambiguous_agents_markers"):
                    install.merge_managed_block(value, "payload")

    def test_remove_deletes_only_the_managed_block(self):
        existing = install.merge_managed_block("before\n", "payload") + "after\n"
        removed = install.remove_managed_block(existing)
        self.assertEqual(removed, "before\nafter\n")


class PackageHygieneTests(unittest.TestCase):
    def test_repository_payload_has_no_live_memory_or_machine_paths(self):
        install.validate_package(install.SOURCE_ROOT)

    def test_repository_guidance_has_no_access_denial_typo(self):
        for path in (
            install.SOURCE_ROOT / "skills/managing-loop-memory/SKILL.md",
            install.SOURCE_ROOT / "runtime/SKILL.md",
        ):
            self.assertNotIn("If the If access", path.read_text(encoding="utf-8"))


class StagingTests(unittest.TestCase):
    def test_stage_contains_codex_runtime_only_and_three_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            stage = Path(temporary) / "stage"
            home.mkdir()
            targets, manifest = install.stage_install(home, stage)
            keys = {target.key for target in targets}
            self.assertEqual(
                keys,
                {
                    ".local/share/loop-memory",
                    ".local/bin/loop-memory",
                    ".codex/skills/managing-loop-memory",
                    ".codex/skills/governing-subagents",
                    ".codex/skills/governing-task-scope",
                    ".codex/AGENTS.md",
                    ".codex/config.toml",
                    ".codex/hooks.json",
                    ".local/state/loop-memory-installer/manifest.json",
                },
            )
            runtime = stage / "trees/runtime"
            self.assertFalse((runtime / "tests").exists())
            self.assertEqual(
                set(manifest["managed"]),
                keys - {".local/state/loop-memory-installer/manifest.json"},
            )

    def test_tree_digest_ignores_runtime_generated_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
            before = install.tree_digest(root)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "value.pyc").write_bytes(b"cache")
            self.assertEqual(install.tree_digest(root), before)

    def test_stage_preserves_existing_network_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            (codex / "config.toml").write_text(
                '[sandbox_workspace_write]\nnetwork_access = false\n',
                encoding="utf-8",
            )
            stage = Path(temporary) / "stage"
            install.stage_install(home, stage)
            staged = (stage / "files/config.toml").read_text(encoding="utf-8")
            self.assertIn("network_access = false", staged)
            self.assertIn('writable_roots = ["~/loop-memory"]', staged)
            parsed = tomllib.loads(staged)
            self.assertEqual(parsed["default_permissions"], "loop-memory")
            self.assertEqual(
                parsed["permissions"]["loop-memory"]["filesystem"]["~/loop-memory"],
                "write",
            )


class TransactionTests(unittest.TestCase):
    def test_publish_is_idempotent_and_second_run_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            first = install.install_files(home)
            backup_root = home / ".local/state/loop-memory-installer/backups"
            first_backups = sorted(backup_root.iterdir())
            second = install.install_files(home)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(sorted(backup_root.iterdir()), first_backups)

    def test_injected_publish_failure_restores_all_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            agents = codex / "AGENTS.md"
            agents.write_text("original agents\n", encoding="utf-8")
            config = codex / "config.toml"
            config.write_text('model = "original"\n', encoding="utf-8")
            with self.assertRaisesRegex(install.InstallerError, "injected_publish_failure"):
                install.install_files(home, fail_after=".codex/AGENTS.md")
            self.assertEqual(agents.read_text(encoding="utf-8"), "original agents\n")
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "original"\n')
            self.assertFalse((home / ".local/share/loop-memory").exists())


class MemoryInitializationTests(unittest.TestCase):
    def test_fresh_install_initializes_canonical_methodology(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            result = install.run_install(home)
            self.assertTrue(result["ok"])
            self.assertTrue(result["memory_initialized"])
            expected = (
                install.SOURCE_ROOT / "global/global-long-methodology.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                (home / "loop-memory/global/long.md").read_text(encoding="utf-8"),
                expected,
            )

    def test_existing_memory_tree_is_byte_identical_after_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            memory = home / "loop-memory"
            memory.mkdir(parents=True)
            sentinel = memory / "sentinel.bin"
            sentinel.write_bytes(b"existing-memory")
            before = install.tree_digest(memory)
            result = install.run_install(home)
            self.assertTrue(result["ok"])
            self.assertFalse(result["memory_initialized"])
            self.assertEqual(install.tree_digest(memory), before)

    def test_reinstall_does_not_change_existing_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            before = install.tree_digest(home / "loop-memory")
            result = install.run_install(home)
            self.assertFalse(result["changed"])
            self.assertFalse(result["memory_initialized"])
            self.assertEqual(install.tree_digest(home / "loop-memory"), before)


class UninstallTests(unittest.TestCase):
    def test_uninstall_restores_previous_codex_default_permission(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            original = (
                'default_permissions = "custom"\n'
                '[permissions.custom]\n'
                'extends = ":workspace"\n'
            )
            (codex / "config.toml").write_text(original, encoding="utf-8")
            install.run_install(home)
            self.assertEqual(
                tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))["default_permissions"],
                "loop-memory",
            )
            install.run_uninstall(home)
            restored = tomllib.loads((codex / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(restored["default_permissions"], "custom")
            self.assertNotIn("loop-memory", restored["permissions"])

    def test_uninstall_removes_only_managed_state_and_preserves_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            (codex / "AGENTS.md").write_text("personal rule\n", encoding="utf-8")
            (codex / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "BeforeTool": [
                                {"hooks": [{"type": "command", "command": "personal"}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            install.run_install(home)
            sentinel = home / "loop-memory/sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            result = install.run_uninstall(home)
            self.assertTrue(result["ok"])
            self.assertEqual(
                (codex / "AGENTS.md").read_text(encoding="utf-8"),
                "personal rule\n",
            )
            hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("BeforeTool", hooks["hooks"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse((home / ".local/share/loop-memory").exists())
            self.assertFalse(
                (home / ".codex/skills/managing-loop-memory").exists()
            )

    def test_uninstall_refuses_modified_managed_tree_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            agents_before = (home / ".codex/AGENTS.md").read_bytes()
            changed = home / ".codex/skills/managing-loop-memory/local.md"
            changed.write_text("local modification\n", encoding="utf-8")
            with self.assertRaisesRegex(install.InstallerError, "managed_tree_modified"):
                install.run_uninstall(home)
            self.assertEqual(
                (home / ".codex/AGENTS.md").read_bytes(),
                agents_before,
            )
            self.assertTrue(changed.is_file())


class UpgradeTests(unittest.TestCase):
    def test_upgrade_requires_an_existing_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            with self.assertRaisesRegex(
                install.InstallerError,
                "upgrade_requires_installation",
            ):
                install.run_upgrade(home)

    def test_upgrade_is_idempotent_and_preserves_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            sentinel = home / "loop-memory/sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            result = install.run_upgrade(home)
            self.assertFalse(result["changed"])
            self.assertTrue(result["memory_preserved"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_upgrade_restores_a_missing_managed_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            launcher = home / ".local/bin/loop-memory"
            launcher.unlink()
            result = install.run_upgrade(home)
            self.assertTrue(result["changed"])
            self.assertTrue(launcher.is_file())

    def test_upgrade_accepts_a_manifest_from_before_a_new_skill_was_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            manifest_path = (
                home / ".local/state/loop-memory-installer/manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["managed"][".codex/skills/governing-task-scope"]
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            skill = home / ".codex/skills/governing-task-scope"
            shutil.rmtree(skill)
            result = install.run_upgrade(home)
            self.assertTrue(result["changed"])
            self.assertTrue(skill.is_dir())

    def test_upgrade_preserves_user_config_changes_and_refreshes_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            config = home / ".codex/config.toml"
            config.write_text(
                'model = "user-choice"\n' + config.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            first = install.run_upgrade(home)
            second = install.run_upgrade(home)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertIn(
                'model = "user-choice"',
                config.read_text(encoding="utf-8"),
            )

    def test_upgrade_refuses_a_modified_managed_tree_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            install.run_install(home)
            agents_before = (home / ".codex/AGENTS.md").read_bytes()
            changed = home / ".codex/skills/managing-loop-memory/local.md"
            changed.write_text("local modification\n", encoding="utf-8")
            with self.assertRaisesRegex(
                install.InstallerError,
                "managed_tree_modified",
            ):
                install.run_upgrade(home)
            self.assertEqual(
                (home / ".codex/AGENTS.md").read_bytes(),
                agents_before,
            )
            self.assertTrue(changed.is_file())


class CliTests(unittest.TestCase):
    def run_cli(
        self, home: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(install.SOURCE_ROOT / "install.py"), *arguments],
            cwd=install.SOURCE_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )

    def test_cli_fresh_install_returns_safe_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            completed = self.run_cli(home)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout,
                "OK action=install changed=true memory_initialized=true "
                "codex_trust_review=required\n",
            )
            self.assertNotIn(str(home), completed.stdout + completed.stderr)

    def test_cli_uninstall_preserves_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            self.assertEqual(self.run_cli(home).returncode, 0)
            completed = self.run_cli(home, "--uninstall")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout,
                "OK action=uninstall changed=true memory_preserved=true\n",
            )
            self.assertTrue((home / "loop-memory").is_dir())

    def test_cli_upgrade_returns_safe_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            self.assertEqual(self.run_cli(home).returncode, 0)
            completed = self.run_cli(home, "--upgrade")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout,
                "OK action=upgrade changed=false memory_preserved=true "
                "codex_trust_review=unchanged\n",
            )
            self.assertNotIn(str(home), completed.stdout + completed.stderr)

    def test_cli_upgrade_and_uninstall_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            completed = self.run_cli(home, "--upgrade", "--uninstall")
            self.assertEqual(completed.returncode, 2)

    def test_verification_failure_restores_installer_owned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            agents = codex / "AGENTS.md"
            agents.write_text("before\n", encoding="utf-8")
            with mock.patch.object(
                install,
                "_verify_static_install",
                side_effect=install.InstallerError(
                    "injected_verification_failure"
                ),
            ):
                with self.assertRaisesRegex(
                    install.InstallerError,
                    "injected_verification_failure",
                ):
                    install.run_install(home)
            self.assertEqual(agents.read_text(encoding="utf-8"), "before\n")
            self.assertFalse((home / ".local/share/loop-memory").exists())


if __name__ == "__main__":
    unittest.main()
