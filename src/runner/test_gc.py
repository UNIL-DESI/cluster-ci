import unittest
import os
import json
import shutil
from pathlib import Path
import time
import sys
import fcntl
import multiprocessing

# Add src to path to import gc_orchestrator
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from src.runner import gc_orchestrator

def run_update_running(project, test_base_dir, test_repo_dir):
    # Setup same environment in child process
    gc_orchestrator.get_base_dir = lambda: test_base_dir
    gc_orchestrator.get_repositories_dir = lambda: test_repo_dir
    gc_orchestrator.update_running(project)

class TestGC(unittest.TestCase):
    def setUp(self):
        self.test_base_dir = Path("test_gc_root").resolve()
        self.test_repo_dir = self.test_base_dir / "repositories"
        self.test_repo_dir.mkdir(parents=True, exist_ok=True)

        # Patch gc_orchestrator functions to use test dirs
        self.original_get_base_dir = gc_orchestrator.get_base_dir
        self.original_get_repositories_dir = gc_orchestrator.get_repositories_dir
        gc_orchestrator.get_base_dir = lambda: self.test_base_dir
        gc_orchestrator.get_repositories_dir = lambda: self.test_repo_dir

    def tearDown(self):
        # Restore original functions
        gc_orchestrator.get_base_dir = self.original_get_base_dir
        gc_orchestrator.get_repositories_dir = self.original_get_repositories_dir
        if self.test_base_dir.exists():
            shutil.rmtree(self.test_base_dir)

    def test_registry_lifecycle(self):
        project = "user/repo"
        gc_orchestrator.update_running(project)

        with open(gc_orchestrator.get_registry_path(), "r") as f:
            registry = json.load(f)
        self.assertIn(project, registry)
        self.assertEqual(registry[project]["status"], "running")

        # Create a dummy file to have some size
        project_path = self.test_repo_dir / project
        project_path.mkdir(parents=True, exist_ok=True)
        with open(project_path / "data.txt", "w") as f:
            f.write("hello world")

        gc_orchestrator.update_idle(project, str(project_path))
        with open(gc_orchestrator.get_registry_path(), "r") as f:
            registry = json.load(f)
        self.assertEqual(registry[project]["status"], "idle")
        self.assertGreater(registry[project]["size_bytes"], 0)

    def test_concurrency_locking(self):
        project = "concurrency/test"
        # First creation
        gc_orchestrator.update_running(project)

        registry_path = gc_orchestrator.get_registry_path()

        # Manually lock the file
        with open(registry_path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)

            # Start a process that tries to update the registry
            p = multiprocessing.Process(target=run_update_running, args=("other/proj", self.test_base_dir, self.test_repo_dir))
            p.start()

            # Wait a bit, it should be blocked
            time.sleep(0.5)
            self.assertTrue(p.is_alive())

            # Unlock
            fcntl.flock(f, fcntl.LOCK_UN)

            # Now it should finish
            p.join(timeout=2)
            self.assertFalse(p.is_alive())

        with open(registry_path, "r") as f:
            registry = json.load(f)
        self.assertIn("other/proj", registry)

    def test_validation(self):
        with self.assertRaises(ValueError):
            gc_orchestrator.update_running("/absolute/path")
        with self.assertRaises(ValueError):
            gc_orchestrator.update_running("../escape")

    def test_gc_logic_panic(self):
        # Simulate critical space (below 50GB threshold)
        import unittest.mock
        original_disk_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: unittest.mock.MagicMock(free=40 * 1024 * 1024 * 1024, total=1000 * 1024 * 1024 * 1024)

        try:
            projects = ["p1", "p2", "p3"]
            for i, p in enumerate(projects):
                p_path = self.test_repo_dir / p
                p_path.mkdir(parents=True, exist_ok=True)
                with open(p_path / "file.txt", "w") as f:
                    f.write("data")

                gc_orchestrator.update_idle(p, str(p_path))
                # Adjust time
                registry_path = gc_orchestrator.get_registry_path()
                with open(registry_path, "r+") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    registry = json.load(f)
                    registry[p]["last_execution"] = time.time() - (8 * 3600 - i * 3600)
                    f.seek(0)
                    f.truncate()
                    json.dump(registry, f)
                    fcntl.flock(f, fcntl.LOCK_UN)

            gc_orchestrator.update_running("p2")
            gc_orchestrator.run_gc()

            with open(gc_orchestrator.get_registry_path(), "r") as f:
                registry = json.load(f)
            self.assertEqual(registry["p1"]["status"], "deleted")
            self.assertEqual(registry["p3"]["status"], "deleted")
            self.assertEqual(registry["p2"]["status"], "running")

        finally:
            shutil.disk_usage = original_disk_usage

    def test_gc_logic_maintenance(self):
        # Simulate maintenance space (between 50GB and 100GB)
        import unittest.mock
        original_disk_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: unittest.mock.MagicMock(free=70 * 1024 * 1024 * 1024, total=1000 * 1024 * 1024 * 1024)

        try:
            p1_path = self.test_repo_dir / "p1"
            p1_path.mkdir(parents=True, exist_ok=True)
            with open(p1_path / "file.txt", "w") as f: f.write("data")
            gc_orchestrator.update_idle("p1", str(p1_path))

            # Make last_execution older than protect threshold (8 hours ago)
            registry_path = gc_orchestrator.get_registry_path()
            with open(registry_path, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                registry = json.load(f)
                registry["p1"]["last_execution"] = time.time() - (8 * 3600)
                f.seek(0)
                f.truncate()
                json.dump(registry, f)
                fcntl.flock(f, fcntl.LOCK_UN)

            # No remote configured, so it should be evicted immediately
            gc_orchestrator.run_transfer_gc()

            with open(gc_orchestrator.get_registry_path(), "r") as f:
                registry = json.load(f)
            self.assertEqual(registry["p1"]["status"], "deleted")

        finally:
            shutil.disk_usage = original_disk_usage

if __name__ == "__main__":
    unittest.main()
