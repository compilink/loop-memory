import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


RUNTIME = Path(__file__).resolve().parents[1]
LAUNCHER = RUNTIME / "bin" / "loop-memory"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STAGE_SCRIPT = RUNTIME / "scripts" / "stage_user_config.py"


class LauncherTests(unittest.TestCase):
    def install_runtime(self, home: Path) -> Path:
        installed = home / ".local" / "share" / "loop-memory"
        shutil.copytree(
            RUNTIME,
            installed,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        launcher = home / ".local" / "bin" / "loop-memory"
        launcher.parent.mkdir(parents=True)
        shutil.copy2(LAUNCHER, launcher)
        return launcher

    def run_launcher(self, launcher: Path, home: Path, cwd: Path, *args: str):
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        return subprocess.run(
            [str(launcher), *args],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def assert_one_json(self, completed: subprocess.CompletedProcess[str]):
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        return json.loads(completed.stdout)

    def test_launcher_uses_only_user_shared_install_from_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Resolve the fixture root first: the runtime intentionally rejects
            # symlinked ancestors, while macOS temporary paths may begin at a
            # `/var` alias for `/private/var`.
            base = Path(temporary).resolve()
            home = base / "home"
            unrelated = base / "unrelated"
            project = base / "project"
            home.mkdir()
            unrelated.mkdir()
            project.mkdir()
            launcher = self.install_runtime(home)

            help_payload = self.assert_one_json(
                self.run_launcher(launcher, home, unrelated, "--json", "--help")
            )
            self.assertEqual(help_payload["operation"], "help")

            loop_root = home / "loop-memory"
            access_payload = self.assert_one_json(
                self.run_launcher(
                    launcher,
                    home,
                    unrelated,
                    "access-check",
                    "--root",
                    str(loop_root),
                    "--json",
                )
            )
            self.assertEqual(access_payload["operation"], "access-check")

            enter_payload = self.assert_one_json(
                self.run_launcher(
                    launcher,
                    home,
                    unrelated,
                    "enter",
                    "--cwd",
                    str(project),
                    "--session-id",
                    "packaging-session",
                    "--root",
                    str(loop_root),
                    "--json",
                )
            )
            self.assertEqual(enter_payload["operation"], "enter")
            self.assertEqual(enter_payload["root"], str(loop_root))

    def test_launcher_is_portable_and_does_not_import_from_its_source_tree(self):
        body = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn(".local", body)
        self.assertIn("share", body)
        self.assertIn("loop-memory", body)
        self.assertNotIn("comp" + "ilink", body)
        self.assertNotIn("/" + "Users/", body)
        self.assertNotIn("/home/", body)
        self.assertNotIn("__file__", body)

    def test_runtime_docs_match_user_level_authority_contract(self):
        main = (RUNTIME / "SKILL.md").read_text(encoding="utf-8")
        operations = (RUNTIME / "references" / "operations.md").read_text(encoding="utf-8")
        readme = (RUNTIME / "README.md").read_text(encoding="utf-8")
        corpus = main + "\n" + operations
        self.assertIn("~/loop-memory", corpus)
        self.assertNotIn("~/.codex/loop-memory", main)
        self.assertLessEqual(operations.count("~/.codex/loop-memory"), 2)
        self.assertRegex(operations, r"one-time\s+legacy relocation|legacy relocation\s+source")
        self.assertNotRegex(corpus, r"0o700|0o600|chmod|fchmod|chown|setfacl|ACL")
        self.assertNotRegex(corpus, r"\bGit\b|\bgit\b")
        self.assertRegex(corpus, r"External legacy sources are read-only")
        self.assertRegex(corpus, r"environment_access_denied")
        self.assertIn("codex_trust_review=required", readme)

    def test_runtime_docs_describe_global_fact_index_and_organization(self):
        main = (RUNTIME / "SKILL.md").read_text(encoding="utf-8")
        operations = (RUNTIME / "references" / "operations.md").read_text(encoding="utf-8")
        readme = (RUNTIME / "README.md").read_text(encoding="utf-8")
        corpus = main + "\n" + operations + "\n" + readme
        for required in (
            "global/facts/index.md",
            "global-fact",
            "global-organize",
            "global_long_organization_due",
            "same task",
            "archive",
        ):
            self.assertIn(required, corpus)


class ConfigurationMergeTests(unittest.TestCase):
    def setUp(self):
        from scripts.loopmem.configuration import (
            merge_claude_settings,
            merge_codex_config,
            merge_codex_hooks,
            merge_codex_writable_root,
            remove_codex_hooks,
            remove_codex_writable_root,
        )

        self.merge_codex_config = merge_codex_config
        self.merge_codex_hooks = merge_codex_hooks
        self.merge_codex_writable_root = merge_codex_writable_root
        self.remove_codex_hooks = remove_codex_hooks
        self.remove_codex_writable_root = remove_codex_writable_root
        self.merge_claude_settings = merge_claude_settings

    def fixture_text(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_codex_toml_merge_preserves_existing_settings_and_unknown_keys(self):
        source = self.fixture_text("codex-config-sanitized.toml")
        before = tomllib.loads(source)
        merged = self.merge_codex_config(source)
        after = tomllib.loads(merged)

        self.assertEqual(set(after), set(before))
        self.assertEqual(after["model"], before["model"])
        self.assertEqual(after["future"], before["future"])
        self.assertEqual(
            after["sandbox_workspace_write"]["unrelated"],
            before["sandbox_workspace_write"]["unrelated"],
        )
        self.assertIs(after["sandbox_workspace_write"]["network_access"], True)
        roots = after["sandbox_workspace_write"]["writable_roots"]
        self.assertEqual(roots.count("~/loop-memory"), 1)
        self.assertNotIn("~/loop-memory/", roots)
        self.assertNotIn("default_permissions", after)
        self.assertEqual(self.merge_codex_config(merged), merged)

    def test_codex_config_merge_creates_missing_owned_section(self):
        source = "model = \"fixture-model\"\n[future]\nenabled = true\n"
        merged = self.merge_codex_config(source)
        parsed = tomllib.loads(merged)
        self.assertEqual(set(parsed), {"model", "future", "sandbox_workspace_write"})
        self.assertTrue(parsed["sandbox_workspace_write"]["network_access"])
        self.assertEqual(parsed["sandbox_workspace_write"]["writable_roots"], ["~/loop-memory"])

    def test_installer_root_merge_preserves_network_access_and_unknown_keys(self):
        source = (
            'model = "fixture"\n'
            '[sandbox_workspace_write]\n'
            'network_access = false\n'
            'unrelated = "keep"\n'
            'writable_roots = ["~/existing", "~/loop-memory/"]\n'
        )
        merged = self.merge_codex_writable_root(source)
        parsed = tomllib.loads(merged)
        section = parsed["sandbox_workspace_write"]
        self.assertIs(section["network_access"], False)
        self.assertEqual(section["unrelated"], "keep")
        self.assertEqual(section["writable_roots"], ["~/existing", "~/loop-memory"])
        self.assertEqual(self.merge_codex_writable_root(merged), merged)

    def test_installer_root_merge_does_not_create_network_access(self):
        merged = self.merge_codex_writable_root('model = "fixture"\n')
        parsed = tomllib.loads(merged)
        section = parsed["sandbox_workspace_write"]
        self.assertNotIn("network_access", section)
        self.assertEqual(section["writable_roots"], ["~/loop-memory"])

    def test_installer_root_removal_preserves_section_and_network_access(self):
        source = (
            '[sandbox_workspace_write]\n'
            'network_access = true\n'
            'writable_roots = ["~/existing", "~/loop-memory", "$HOME/loop-memory/"]\n'
        )
        removed = self.remove_codex_writable_root(source)
        section = tomllib.loads(removed)["sandbox_workspace_write"]
        self.assertIs(section["network_access"], True)
        self.assertEqual(section["writable_roots"], ["~/existing"])
        self.assertEqual(self.remove_codex_writable_root(removed), removed)

    def test_installer_hook_removal_preserves_unrelated_siblings(self):
        source = self.merge_codex_hooks({
            "version": 1,
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "fixture"}]}]
            },
        })
        removed = self.remove_codex_hooks(source)
        self.assertEqual(removed["version"], 1)
        self.assertEqual(
            removed["hooks"]["SessionStart"],
            [{"hooks": [{"type": "command", "command": "fixture"}]}],
        )
        self.assertNotIn("SessionEnd", removed["hooks"])
        self.assertNotIn("SubagentStart", removed["hooks"])

    def test_codex_hook_merge_preserves_existing_and_unknown_definitions(self):
        source = json.loads(self.fixture_text("codex-hooks-sanitized.json"))
        merged = self.merge_codex_hooks(source)

        self.assertEqual(set(merged), set(source))
        self.assertEqual(merged["future"], source["future"])
        self.assertEqual(merged["hooks"]["BeforeTool"], source["hooks"]["BeforeTool"])
        for event in ("SessionStart", "SessionEnd", "SubagentStart"):
            encoded = json.dumps(merged["hooks"][event])
            self.assertIn("~/.local/share/loop-memory/adapters/codex_hook.py", encoded)
            expected_timeout = 3 if event == "SessionEnd" else 12
            self.assertIn(f'"timeout": {expected_timeout}', encoded)
        self.assertEqual(self.merge_codex_hooks(merged), merged)

    def test_hook_merge_creates_missing_owned_hook_maps(self):
        codex = self.merge_codex_hooks({"version": 1, "future": {"enabled": True}})
        claude = self.merge_claude_settings({"model": "fixture-model"})
        self.assertEqual(set(codex), {"version", "future", "hooks"})
        self.assertEqual(set(claude), {"model", "hooks"})
        self.assertEqual(set(codex["hooks"]), {"SessionStart", "SessionEnd", "SubagentStart"})
        self.assertEqual(set(claude["hooks"]), {"SessionStart", "SessionEnd"})

    def test_codex_session_end_uses_supported_timeout(self):
        merged = self.merge_codex_hooks({})
        expected = {
            "SessionStart": 12,
            "SessionEnd": 3,
            "SubagentStart": 12,
        }
        for event, timeout in expected.items():
            hook = merged["hooks"][event][0]["hooks"][0]
            self.assertEqual(hook["timeout"], timeout)

    def test_stale_loop_hook_is_bounded_without_changing_sibling_hook(self):
        source = json.loads(self.fixture_text("codex-hooks-sanitized.json"))
        source["hooks"]["SessionEnd"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 ~/.local/share/loop-memory/adapters/codex_hook.py",
                        "timeout": 999,
                    },
                    {"type": "command", "command": "fixture-preserved"},
                ]
            }
        ]
        merged = self.merge_codex_hooks(source)
        encoded = json.dumps(merged["hooks"]["SessionEnd"])
        self.assertEqual(encoded.count("adapters/codex_hook.py"), 1)
        self.assertIn("fixture-preserved", encoded)
        self.assertNotIn('"timeout": 999', encoded)
        self.assertIn('"timeout": 3', encoded)

    def test_exact_loop_hooks_deduplicate_without_touching_substring_sibling(self):
        source = json.loads(self.fixture_text("codex-hooks-sanitized.json"))
        unrelated = {
            "type": "command",
            "command": "echo ~/.local/share/loop-memory/adapters/codex_hook.py",
            "timeout": 41,
            "future": "fixture-preserved",
        }
        exact = {
            "type": "command",
            "command": "python3 ~/.local/share/loop-memory/adapters/codex_hook.py",
            "timeout": 999,
        }
        source["hooks"]["SessionEnd"] = [
            {"matcher": "first", "hooks": [copy.deepcopy(unrelated), copy.deepcopy(exact)]},
            {"matcher": "second", "hooks": [copy.deepcopy(exact), {"type": "command", "command": "fixture-last"}]},
        ]
        merged = self.merge_codex_hooks(source)
        groups = merged["hooks"]["SessionEnd"]
        self.assertEqual(groups[0]["hooks"][0], unrelated)
        self.assertEqual(groups[0]["matcher"], "first")
        self.assertEqual(groups[1]["matcher"], "second")
        self.assertEqual(groups[1]["hooks"], [{"type": "command", "command": "fixture-last"}])
        encoded = json.dumps(groups)
        self.assertEqual(encoded.count("python3 ~/.local/share/loop-memory/adapters/codex_hook.py"), 1)
        self.assertIn('"timeout": 3', encoded)

    def test_claude_hook_merge_preserves_permissions_env_model_and_unknown_keys(self):
        source = json.loads(self.fixture_text("claude-settings-sanitized.json"))
        merged = self.merge_claude_settings(source)

        self.assertEqual(set(merged), set(source))
        for key in ("env", "permissions", "model", "future"):
            self.assertEqual(merged[key], source[key])
        self.assertEqual(merged["hooks"]["PreToolUse"], source["hooks"]["PreToolUse"])
        for event in ("SessionStart", "SessionEnd"):
            encoded = json.dumps(merged["hooks"][event])
            self.assertIn("~/.local/share/loop-memory/adapters/claude_hook.py", encoded)
            self.assertIn('"timeout": 12', encoded)
        self.assertEqual(self.merge_claude_settings(merged), merged)


class StagedConfigurationTests(unittest.TestCase):
    def run_validator(self, source: Path, staged: Path):
        return subprocess.run(
            [
                sys.executable,
                str(RUNTIME / "scripts" / "validate_user_config.py"),
                "--source-config", str(source / "config.toml"),
                "--source-hooks", str(source / "hooks.json"),
                "--source-settings", str(source / "settings.json"),
                "--config", str(staged / "config.toml"),
                "--hooks", str(staged / "hooks.json"),
                "--settings", str(staged / "settings.json"),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_validator_rejects_missing_hook_and_noncanonical_root_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            staged = base / "staged"
            source.mkdir()
            staged.mkdir()
            (source / "config.toml").write_text("model = \"fixture\"\n", encoding="utf-8")
            (source / "hooks.json").write_text("{}\n", encoding="utf-8")
            (source / "settings.json").write_text("{}\n", encoding="utf-8")

            valid_config = "[sandbox_workspace_write]\nnetwork_access = true\nwritable_roots = [\"~/loop-memory\"]\n"
            valid_codex = {"hooks": {event: [] for event in ("SessionStart", "SessionEnd", "SubagentStart")}}
            valid_claude = {"hooks": {event: [] for event in ("SessionStart", "SessionEnd")}}
            (staged / "config.toml").write_text(valid_config, encoding="utf-8")
            (staged / "hooks.json").write_text(json.dumps(valid_codex), encoding="utf-8")
            (staged / "settings.json").write_text(json.dumps(valid_claude), encoding="utf-8")

            missing = json.loads((staged / "hooks.json").read_text(encoding="utf-8"))
            del missing["hooks"]["SubagentStart"]
            (staged / "hooks.json").write_text(json.dumps(missing), encoding="utf-8")
            self.assertNotEqual(self.run_validator(source, staged).returncode, 0)
            (staged / "hooks.json").write_text(json.dumps(valid_codex), encoding="utf-8")

            for invalid in (
                "[sandbox_workspace_write]\nnetwork_access = false\nwritable_roots = [\"~/loop-memory\"]\n",
                "[sandbox_workspace_write]\nnetwork_access = true\nwritable_roots = [\"~/loop-memory\", \"~/loop-memory/\"]\n",
                "[sandbox_workspace_write]\nnetwork_access = true\nwritable_roots = [\"~/loop-memory\", \"~/loop-memory\"]\n",
            ):
                (staged / "config.toml").write_text(invalid, encoding="utf-8")
                self.assertNotEqual(self.run_validator(source, staged).returncode, 0)

    def test_validator_rejects_bad_loop_hook_or_unowned_nested_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            staged = base / "staged"
            source.mkdir()
            staged.mkdir()
            shutil.copy2(FIXTURES / "codex-config-sanitized.toml", source / "config.toml")
            shutil.copy2(FIXTURES / "codex-hooks-sanitized.json", source / "hooks.json")
            shutil.copy2(FIXTURES / "claude-settings-sanitized.json", source / "settings.json")
            completed = subprocess.run(
                [sys.executable, str(STAGE_SCRIPT), "--codex-config", str(source / "config.toml"),
                 "--codex-hooks", str(source / "hooks.json"), "--claude-settings", str(source / "settings.json"),
                 "--output-dir", str(staged)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0)
            originals = {name: (staged / name).read_text(encoding="utf-8") for name in ("config.toml", "hooks.json", "settings.json")}

            for mutation in ("empty", "wrong_command", "wrong_type", "wrong_timeout", "duplicate"):
                value = json.loads(originals["hooks.json"])
                hooks = value["hooks"]["SubagentStart"][0]["hooks"]
                if mutation == "empty":
                    value["hooks"]["SubagentStart"] = []
                elif mutation == "duplicate":
                    hooks.append(copy.deepcopy(hooks[0]))
                else:
                    key = {"wrong_command": "command", "wrong_type": "type", "wrong_timeout": "timeout"}[mutation]
                    hooks[0][key] = {"wrong_command": "fixture-command", "wrong_type": "fixture", "wrong_timeout": 99}[mutation]
                (staged / "hooks.json").write_text(json.dumps(value), encoding="utf-8")
                self.assertNotEqual(self.run_validator(source, staged).returncode, 0, mutation)
                (staged / "hooks.json").write_text(originals["hooks.json"], encoding="utf-8")

            value = json.loads(originals["hooks.json"])
            hooks = value["hooks"]["SessionEnd"][0]["hooks"]
            hooks[0]["type"] = "prompt"
            hooks[0]["timeout"] = 12
            (staged / "hooks.json").write_text(json.dumps(value), encoding="utf-8")
            self.assertNotEqual(self.run_validator(source, staged).returncode, 0)

            value = json.loads(originals["settings.json"])
            value["permissions"]["allow"].append("fixture-drift")
            (staged / "settings.json").write_text(json.dumps(value), encoding="utf-8")
            invalid = self.run_validator(source, staged)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertNotIn("fixture-value", invalid.stdout + invalid.stderr)

    def test_validator_accepts_owned_containers_added_to_minimal_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            staged = base / "staged"
            source.mkdir()
            (source / "config.toml").write_text("model = \"fixture\"\n", encoding="utf-8")
            (source / "hooks.json").write_text("{}\n", encoding="utf-8")
            (source / "settings.json").write_text('{"model":"fixture"}\n', encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(STAGE_SCRIPT), "--codex-config", str(source / "config.toml"),
                 "--codex-hooks", str(source / "hooks.json"), "--claude-settings", str(source / "settings.json"),
                 "--output-dir", str(staged)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(self.run_validator(source, staged).returncode, 0)

    def test_validator_preserves_non_loop_siblings_inside_owned_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            staged = base / "staged"
            source.mkdir()
            shutil.copy2(FIXTURES / "codex-config-sanitized.toml", source / "config.toml")
            hooks = json.loads((FIXTURES / "codex-hooks-sanitized.json").read_text())
            hooks["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "fixture-sibling"}]}]
            (source / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            shutil.copy2(FIXTURES / "claude-settings-sanitized.json", source / "settings.json")
            subprocess.run([sys.executable, str(STAGE_SCRIPT), "--codex-config", str(source / "config.toml"),
                            "--codex-hooks", str(source / "hooks.json"), "--claude-settings", str(source / "settings.json"),
                            "--output-dir", str(staged)], check=True, capture_output=True)
            value = json.loads((staged / "hooks.json").read_text())
            value["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "fixture-drift"
            (staged / "hooks.json").write_text(json.dumps(value), encoding="utf-8")
            self.assertNotEqual(self.run_validator(source, staged).returncode, 0)

    def test_validator_rejects_extra_owned_identity_even_with_wrong_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            staged = base / "staged"
            source.mkdir()
            shutil.copy2(FIXTURES / "codex-config-sanitized.toml", source / "config.toml")
            shutil.copy2(FIXTURES / "codex-hooks-sanitized.json", source / "hooks.json")
            shutil.copy2(FIXTURES / "claude-settings-sanitized.json", source / "settings.json")
            subprocess.run([sys.executable, str(STAGE_SCRIPT), "--codex-config", str(source / "config.toml"),
                            "--codex-hooks", str(source / "hooks.json"), "--claude-settings", str(source / "settings.json"),
                            "--output-dir", str(staged)], check=True, capture_output=True)
            value = json.loads((staged / "hooks.json").read_text())
            duplicate = copy.deepcopy(value["hooks"]["SessionEnd"][0]["hooks"][0])
            duplicate["timeout"] = 999
            value["hooks"]["SessionEnd"][0]["hooks"].append(duplicate)
            (staged / "hooks.json").write_text(json.dumps(value), encoding="utf-8")
            self.assertNotEqual(self.run_validator(source, staged).returncode, 0)

    def test_validator_rejects_drift_in_owned_hook_extra_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            staged = base / "staged"
            source.mkdir()
            shutil.copy2(FIXTURES / "codex-config-sanitized.toml", source / "config.toml")
            hooks = json.loads((FIXTURES / "codex-hooks-sanitized.json").read_text())
            for event in ("SessionStart", "SessionEnd", "SubagentStart"):
                hooks["hooks"][event] = [{"hooks": [{"type": "command", "command": "python3 ~/.local/share/loop-memory/adapters/codex_hook.py", "timeout": 12, "future": "ORIGINAL"}]}]
            (source / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            shutil.copy2(FIXTURES / "claude-settings-sanitized.json", source / "settings.json")
            subprocess.run([sys.executable, str(STAGE_SCRIPT), "--codex-config", str(source / "config.toml"),
                            "--codex-hooks", str(source / "hooks.json"), "--claude-settings", str(source / "settings.json"),
                            "--output-dir", str(staged)], check=True, capture_output=True)
            value = json.loads((staged / "hooks.json").read_text())
            value["hooks"]["SessionStart"][0]["hooks"][0]["future"] = "CHANGED"
            (staged / "hooks.json").write_text(json.dumps(value), encoding="utf-8")
            result = self.run_validator(source, staged)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("ORIGINAL", result.stdout + result.stderr)
            self.assertNotIn("CHANGED", result.stdout + result.stderr)

    def test_stager_writes_parseable_artifacts_and_outputs_only_safe_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "staged"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(STAGE_SCRIPT),
                    "--codex-config",
                    str(FIXTURES / "codex-config-sanitized.toml"),
                    "--codex-hooks",
                    str(FIXTURES / "codex-hooks-sanitized.json"),
                    "--claude-settings",
                    str(FIXTURES / "claude-settings-sanitized.json"),
                    "--output-dir",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(
                completed.stdout,
                "OK root=~/loop-memory "
                "codex_hooks=SessionEnd,SessionStart,SubagentStart "
                "claude_hooks=SessionEnd,SessionStart "
                "codex_trust_review=required\n",
            )
            self.assertNotIn("fixture-value", completed.stdout)
            self.assertEqual(
                set(path.name for path in output.iterdir()),
                {"config.toml", "hooks.json", "settings.json"},
            )
            tomllib.loads((output / "config.toml").read_text(encoding="utf-8"))
            json.loads((output / "hooks.json").read_text(encoding="utf-8"))
            json.loads((output / "settings.json").read_text(encoding="utf-8"))

            validated = subprocess.run(
                [
                    sys.executable,
                    str(RUNTIME / "scripts" / "validate_user_config.py"),
                    "--source-config",
                    str(FIXTURES / "codex-config-sanitized.toml"),
                    "--source-hooks",
                    str(FIXTURES / "codex-hooks-sanitized.json"),
                    "--source-settings",
                    str(FIXTURES / "claude-settings-sanitized.json"),
                    "--config",
                    str(output / "config.toml"),
                    "--hooks",
                    str(output / "hooks.json"),
                    "--settings",
                    str(output / "settings.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(validated.stderr, "")
            self.assertEqual(
                validated.stdout,
                "OK root=~/loop-memory "
                "codex_hooks=SessionEnd,SessionStart,SubagentStart "
                "claude_hooks=SessionEnd,SessionStart "
                "codex_trust_review=required\n",
            )
            self.assertNotIn("fixture-value", validated.stdout)


if __name__ == "__main__":
    unittest.main()
