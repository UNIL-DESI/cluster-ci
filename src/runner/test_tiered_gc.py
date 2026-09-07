import unittest
import unittest.mock as mock
import os
import shutil
from pathlib import Path
import sys

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.runner import gc_orchestrator

class TestTieredGC(unittest.TestCase):
    def setUp(self):
        self.test_base_dir = Path("test_tiered_gc_root").resolve()
        self.test_repo_dir = self.test_base_dir / "repositories"
        self.test_repo_dir.mkdir(parents=True, exist_ok=True)

        self.original_get_base_dir = gc_orchestrator.get_base_dir
        self.original_get_repositories_dir = gc_orchestrator.get_repositories_dir
        gc_orchestrator.get_base_dir = lambda: self.test_base_dir
        gc_orchestrator.get_repositories_dir = lambda: self.test_repo_dir

    def tearDown(self):
        gc_orchestrator.get_base_dir = self.original_get_base_dir
        gc_orchestrator.get_repositories_dir = self.original_get_repositories_dir
        if self.test_base_dir.exists():
            shutil.rmtree(self.test_base_dir)

    @mock.patch("subprocess.run")
    def test_cleanup_level_1(self, mock_run):
        project_path = self.test_repo_dir / "proj1"
        project_path.mkdir(parents=True)
        gc_orchestrator.cleanup_level_1(project_path, "proj1")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(Path(args[0]).name, "dvc")
        self.assertIn("gc", args)
        self.assertIn("--rev", args)

    def test_cleanup_level_2(self):
        project_path = self.test_repo_dir / "proj1"
        project_path.mkdir(parents=True)
        large_file = project_path / "large.log"
        with open(large_file, "wb") as f:
            f.seek(501 * 1024 * 1024)
            f.write(b"0")

        small_file = project_path / "small.txt"
        with open(small_file, "wb") as f:
            f.write(b"hello")

        git_dir = project_path / ".git"
        git_dir.mkdir()
        large_git_file = git_dir / "large_git"
        with open(large_git_file, "wb") as f:
            f.seek(600 * 1024 * 1024)
            f.write(b"0")

        gc_orchestrator.cleanup_level_2(project_path, "proj1")

        self.assertFalse(large_file.exists())
        self.assertTrue(small_file.exists())
        self.assertTrue(large_git_file.exists())

    def test_cleanup_level_3(self):
        # Level 3 is Docker volume removal in orchestrator
        project_name = "proj1"
        with mock.patch("subprocess.run") as mock_run:
            gc_orchestrator.cleanup_level_3(Path("/tmp/fake"), project_name)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertIn("docker", args)
            self.assertIn("volume", args)
            self.assertIn("rm", args)
            self.assertTrue(any(f"cluster-ci-home-{project_name}" in arg for arg in args))

    def test_cleanup_level_4(self):
        # Level 4 is DVC cache removal
        project_path = self.test_repo_dir / "proj1"
        dvc_cache = project_path / ".dvc" / "cache"
        dvc_cache.mkdir(parents=True)
        (dvc_cache / "some_data").touch()

        gc_orchestrator.cleanup_level_4(project_path, "proj1")
        self.assertFalse(dvc_cache.exists())
        self.assertTrue((project_path / ".dvc").exists())

    def test_cleanup_level_5(self):
        # Level 5 is entire directory removal
        project_path = self.test_repo_dir / "proj1"
        project_path.mkdir(parents=True)
        (project_path / "file.txt").touch()

        gc_orchestrator.cleanup_level_5(project_path, "proj1")
        self.assertFalse(project_path.exists())

if __name__ == "__main__":
    unittest.main()
