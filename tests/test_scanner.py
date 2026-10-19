import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mac_disk_scanner.actions import ActionRunner
from mac_disk_scanner.scanner import Scanner
from mac_disk_scanner.utils import CACHEDIR_TAG_SIGNATURE

MIB = 1024 * 1024


def tag(path: Path, signature: bytes = CACHEDIR_TAG_SIGNATURE) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "CACHEDIR.TAG").write_bytes(signature + b"\n")


def blob(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(os.urandom(size))


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class CacheTagTest(TempDirTest):
    def setUp(self):
        super().setUp()
        # A temp base that is not the parent of anything below, so the
        # temporary-directory rule does not mask the rule under test.
        self.scanner = Scanner(tmp_base=self.root / "unused-tmp")

    def test_tagged_directory_outside_project_is_build_cache(self):
        tag(self.root / "review" / "target_arb")
        node = self.scanner.make_real_node(
            self.root / "review" / "target_arb", str(self.root / "review"),
            measure_size=False,
        )
        self.assertEqual(node.metadata["action_type"], "delete_cache")
        self.assertEqual(node.category, "Build cache")

    def test_tagged_directory_inside_project_belongs_to_project_clean(self):
        project = self.root / "proj"
        project.mkdir()
        (project / "Cargo.toml").write_text("[package]\nname='p'\n")
        tag(project / "target.noindex")
        node = self.scanner.make_real_node(
            project / "target.noindex", str(project), measure_size=False,
        )
        self.assertEqual(node.metadata["action_type"], "none")
        self.assertIn("cargo clean", node.reason)

    def test_wrong_signature_is_not_a_cache(self):
        tag(self.root / "parent" / "fake", signature=b"Signature: nope")
        node = self.scanner.make_real_node(
            self.root / "parent" / "fake", str(self.root / "parent"),
            measure_size=False,
        )
        self.assertNotEqual(node.category, "Build cache")
        self.assertNotEqual(node.metadata["action_type"], "delete_cache")


class TemporaryRegionTest(TempDirTest):
    def test_lists_large_owned_items_regardless_of_name(self):
        blob(self.root / "simplify_review" / "target" / "big.bin", 11 * MIB)
        tag(self.root / "simplify_review" / "target")
        blob(self.root / "anything-goes" / "data.bin", 11 * MIB)
        blob(self.root / "small" / "data.bin", 1 * MIB)
        blob(self.root / "big.log", 11 * MIB)
        os.symlink(self.root / "anything-goes", self.root / "link")

        region = Scanner(tmp_base=self.root).scan_private_tmp()
        by_name = {c.name: c for c in region.children}

        self.assertEqual(set(by_name), {"simplify_review", "anything-goes", "big.log"})
        self.assertEqual(by_name["simplify_review"].category, "Build cache")
        self.assertEqual(by_name["simplify_review"].metadata["action_type"], "delete_cache")
        self.assertEqual(by_name["anything-goes"].category, "Temporary")
        self.assertEqual(by_name["big.log"].metadata["action_type"], "delete_manual")
        self.assertGreaterEqual(by_name["simplify_review"].size, 11 * MIB)
        self.assertGreaterEqual(region.size, 33 * MIB)

    def test_marks_directories_with_running_processes(self):
        busy = self.root / "busy"
        blob(busy / "data.bin", 11 * MIB)
        proc = subprocess.Popen(["/bin/sleep", "30"], cwd=busy)
        try:
            region = Scanner(tmp_base=self.root).scan_private_tmp()
            node = region.children[0]
            self.assertTrue(any("sleep" in u for u in node.metadata["in_use_by"]))
            with self.assertRaisesRegex(RuntimeError, "in use"):
                ActionRunner().execute(node)
            self.assertTrue(busy.exists())
        finally:
            proc.kill()
            proc.wait()


class WorktreeRegionTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / ".gitignore").write_text("target/\n")
        (self.repo / "Cargo.toml").write_text("[package]\nname='p'\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        self.wt = self.root / "elsewhere" / "wt"
        git("worktree", "add", "-q", "-b", "feature", str(self.wt), cwd=self.repo)
        blob(self.wt / "target" / "debug" / "app", 2 * MIB)

    def scan(self):
        return Scanner().scan_worktrees(project_paths=[self.repo])

    def test_clean_worktree_is_removable_with_ignored_output(self):
        region = self.scan()
        self.assertEqual(len(region.children), 1)
        node = region.children[0]
        self.assertEqual(Path(node.path).resolve(), self.wt.resolve())
        self.assertEqual(node.metadata["worktree"]["branch"], "feature")
        self.assertEqual(node.metadata["action_type"], "worktree_remove")

        ActionRunner().execute(node)

        self.assertFalse(self.wt.exists())
        self.assertIn("feature", git("branch", cwd=self.repo))

    def test_worktree_with_untracked_work_is_not_removable(self):
        (self.wt / "notes.txt").write_text("unsaved idea")
        node = self.scan().children[0]
        self.assertEqual(node.metadata["action_type"], "none")
        self.assertEqual(node.metadata["worktree"]["changes"], 1)

    def test_git_refuses_removal_if_work_appears_after_scan(self):
        node = self.scan().children[0]
        (self.wt / "notes.txt").write_text("written after the scan")
        with self.assertRaisesRegex(RuntimeError, "worktree remove failed"):
            ActionRunner().execute(node)
        self.assertTrue((self.wt / "notes.txt").exists())


if __name__ == "__main__":
    unittest.main()
