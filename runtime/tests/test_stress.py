import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop_memory.py"


class ProcessStressTests(unittest.TestCase):
    def run_cli(self, home: Path, *arguments: object) -> tuple[subprocess.CompletedProcess[str], dict]:
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *(str(value) for value in arguments), "--json"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        return completed, json.loads(completed.stdout)

    def enter(self, home: Path, project: Path, root: Path, host: str):
        return self.run_cli(
            home,
            "enter",
            "--cwd",
            project,
            "--session-id",
            host,
            "--root",
            root,
        )

    def assert_success_or_busy(self, result: tuple[subprocess.CompletedProcess[str], dict]):
        completed, payload = result
        if completed.returncode == 0:
            self.assertTrue(payload["ok"])
        else:
            self.assertIn(
                (completed.returncode, payload["error"]["code"]),
                {(3, "busy"), (3, "lease_busy"), (4, "conversion_conflict")},
                f"unexpected concurrent result: {payload}",
            )
            self.assertFalse(payload["ok"])

    def assert_no_residue(self, root: Path):
        self.assertEqual(list(root.rglob("*.tmp")), [])
        self.assertEqual(list(root.rglob("*.lock")), [])
        for path in root.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_parallel_enter_converges_same_and_different_host_sessions(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            home = Path(temporary)
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            hosts = [f"host-{index % 4}" for index in range(16)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                raced = list(pool.map(lambda host: self.enter(home, project, root, host), hosts))
            for result in raced:
                self.assert_success_or_busy(result)

            resolved: dict[str, str] = {}
            for host in sorted(set(hosts)):
                completed, payload = self.enter(home, project, root, host)
                self.assertEqual(completed.returncode, 0)
                resolved[host] = payload["session_id"]
                repeated, repeated_payload = self.enter(home, project, root, host)
                self.assertEqual(repeated.returncode, 0)
                self.assertEqual(repeated_payload["session_id"], resolved[host])
            self.assertEqual(len(set(resolved.values())), len(resolved))
            self.assert_no_residue(root)

    def test_parallel_session_writes_and_promotions_are_whole_and_idempotent(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            home = Path(temporary)
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            completed, entered = self.enter(home, project, root, "host")
            self.assertEqual(completed.returncode, 0)

            status_paths = []
            status_bodies = []
            for index in range(12):
                body = f"# Session Status\n\ncomplete-status-{index}\n"
                path = home / f"status-{index}.md"
                path.write_text(body, encoding="utf-8")
                status_paths.append(path)
                status_bodies.append(body)

            def write_status(path: Path):
                return self.run_cli(
                    home,
                    "session-write",
                    "--cwd",
                    project,
                    "--thread-id",
                    "host",
                    "--kind",
                    "status",
                    "--input",
                    path,
                    "--root",
                    root,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                writes = list(pool.map(write_status, status_paths))
            for result in writes:
                self.assert_success_or_busy(result)
            self.assertIn(
                Path(entered["paths"]["status"]).read_text(encoding="utf-8"),
                status_bodies,
            )

            entries = []
            for index in range(8):
                body = (
                    f"- [2026-08-14][verified] Stress candidate {index}.\n"
                    f"  Evidence: process stress fixture {index}.\n"
                )
                path = home / f"entry-{index}.md"
                path.write_text(body, encoding="utf-8")
                entries.append(path)

            def promote(path: Path):
                return self.run_cli(
                    home,
                    "promote",
                    "--cwd",
                    project,
                    "--thread-id",
                    "host",
                    "--scope",
                    "project",
                    "--section",
                    "Verified Facts",
                    "--input",
                    path,
                    "--root",
                    root,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                promotions = list(pool.map(promote, entries))
            for result in promotions:
                self.assert_success_or_busy(result)
            for path in entries:
                serial, payload = promote(path)
                self.assertEqual(serial.returncode, 0)
                self.assertTrue(payload["ok"])
            memory = Path(entered["paths"]["project_memory"]).read_text(encoding="utf-8")
            for index in range(8):
                self.assertEqual(memory.count(f"Stress candidate {index}."), 1)
            self.assert_no_residue(root)

    def test_parallel_enter_recovers_one_expired_dead_registry_lease(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            home = Path(temporary)
            project = home / "project"
            project.mkdir()
            root = home / "loop"
            first, _ = self.enter(home, project, root, "host-0")
            self.assertEqual(first.returncode, 0)
            lock = root / "locks" / "registry.lock"
            lock.write_text(
                json.dumps(
                    {
                        "owner": "dead-stress-owner",
                        "pid": 99999999,
                        "acquired_at": 1,
                        "expires_at": 2,
                        "token": "deadbeef",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                raced = list(
                    pool.map(
                        lambda index: self.enter(home, project, root, f"host-{index % 3}"),
                        range(8),
                    )
                )
            for result in raced:
                self.assert_success_or_busy(result)
            for host in ("host-0", "host-1", "host-2"):
                completed, payload = self.enter(home, project, root, host)
                self.assertEqual(completed.returncode, 0)
                self.assertTrue(payload["ok"])
            self.assert_no_residue(root)


if __name__ == "__main__":
    unittest.main()
