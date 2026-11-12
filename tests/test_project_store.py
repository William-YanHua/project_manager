"""Behavior checks for project isolation, persistence, and concurrent writes."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/project-manager/scripts/project_store.py"


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.base = Path(self.workspace.name)
        self.a = self.base / "project-a"
        self.b = self.base / "project-b"
        self.a.mkdir()
        self.b.mkdir()
        self.environment = {**os.environ, "PROJECT_MANAGER_HUB_DIR": str(self.base / "hub"),
                            "PYTHONDONTWRITEBYTECODE": "1"}

    def run_command(self, command, root, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project-root", str(root), *map(str, args)],
            capture_output=True, text=True, env=self.environment,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def replacement(self, content, name="replacement.md"):
        path = self.base / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_isolation_and_resume(self):
        a = self.run_command("init", self.a)
        b = self.run_command("init", self.b)
        self.assertNotEqual(a["project_id"], b["project_id"])
        content = "# 项目 A\n\n| T001 | 需求确认 | 已完成 | 用户已确认 |\n"
        updated = self.run_command("write", self.a, "--input", self.replacement(content),
                                   "--expected-sha256", a["sha256"])
        self.assertEqual(updated["content"], content)
        self.assertEqual(self.run_command("read", self.b), b)
        self.assertEqual(self.run_command("init", self.a), updated)
        self.assertEqual(self.run_command("read", self.a), updated)

    def test_stale_write_does_not_overwrite_new_progress(self):
        original = self.run_command("init", self.a)
        updated = self.run_command("write", self.a, "--input", self.replacement("# 新进展\n"),
                                   "--expected-sha256", original["sha256"])
        self.run_command("write", self.a, "--input", self.replacement("# 旧内容\n"),
                         "--expected-sha256", original["sha256"], success=False)
        self.assertEqual(self.run_command("read", self.a), updated)

    def test_concurrent_writers_only_one_succeeds(self):
        original = self.run_command("init", self.a)
        files = [self.replacement(f"# 更新 {i}\n", f"{i}.md") for i in range(2)]

        def write(path):
            return subprocess.run(
                [sys.executable, str(SCRIPT), "write", "--project-root", str(self.a),
                 "--input", str(path), "--expected-sha256", original["sha256"]],
                capture_output=True, text=True, env=self.environment,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, files))
        self.assertEqual(sorted(result.returncode for result in results), [0, 1])
        winner = next(json.loads(result.stdout) for result in results if result.returncode == 0)
        self.assertEqual(self.run_command("read", self.a), winner)

    def test_paths_cannot_escape_project(self):
        self.run_command("init", self.a)
        config_path = self.a / ".project-manager.json"
        config = json.loads(config_path.read_text())
        outside = self.b / "outside.md"
        outside.write_text("unchanged")
        for path in ("../project-b/outside.md", str(outside)):
            with self.subTest(path=path):
                config["record"] = path
                config_path.write_text(json.dumps(config))
                self.run_command("read", self.a, success=False)
                self.run_command("write", self.a, "--input", self.replacement("# changed"),
                                 "--expected-sha256", "invalid", success=False)
                self.assertEqual(outside.read_text(), "unchanged")

    def test_symlink_record_is_rejected(self):
        original = self.run_command("init", self.a)
        record = Path(original["record_path"])
        record.unlink()
        outside = self.b / "outside.md"
        outside.write_text("unchanged")
        record.symlink_to(outside)
        self.run_command("read", self.a, success=False)
        self.run_command("write", self.a, "--input", self.replacement("# changed"),
                         "--expected-sha256", original["sha256"], success=False)
        self.assertEqual(outside.read_text(), "unchanged")

    def test_initializer_rejects_symlink_directory(self):
        (self.a / "docs").symlink_to(self.b, target_is_directory=True)
        self.run_command("init", self.a, success=False)
        self.assertEqual(list(self.b.iterdir()), [])
        self.assertFalse((self.a / ".project-manager.json").exists())

    def test_existing_or_missing_record_is_not_reset(self):
        record = self.a / "docs/project-management/PROJECT.md"
        record.parent.mkdir(parents=True)
        record.write_text("existing")
        self.run_command("init", self.a, success=False)
        self.assertEqual(record.read_text(), "existing")
        record.unlink()
        original = self.run_command("init", self.a)
        Path(original["record_path"]).unlink()
        self.run_command("init", self.a, success=False)
        self.assertFalse(record.exists())

    def test_empty_and_invalid_utf8_updates_preserve_record(self):
        original = self.run_command("init", self.a)
        for data in (b" \n", b"\xff"):
            with self.subTest(data=data):
                path = self.base / "bad.md"
                path.write_bytes(data)
                self.run_command("write", self.a, "--input", path,
                                 "--expected-sha256", original["sha256"], success=False)
                self.assertEqual(self.run_command("read", self.a), original)

    def test_corrupt_config_is_not_reinitialized(self):
        path = self.a / ".project-manager.json"
        path.write_text("broken")
        self.run_command("init", self.a, success=False)
        self.assertEqual(path.read_text(), "broken")


if __name__ == "__main__":
    unittest.main()
