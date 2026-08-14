import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.cluster.cluster_run import package_local_source


class TestLocalSourcePackaging(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = Path(self.temp_dir.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.project, check=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_explicitly_includes_ignored_directory_but_never_env_file(self):
        (self.project / '.gitignore').write_text('private-data/\n.env\n')
        (self.project / '.cluster-ci-local-include').write_text('private-data\n.env\n')
        (self.project / 'private-data').mkdir()
        (self.project / 'private-data' / 'case.txt').write_text('confidential input')
        (self.project / '.env').write_text('SECRET=not-for-worker\n')

        archive_path = package_local_source(self.project)
        try:
            with tarfile.open(archive_path, 'r:gz') as archive:
                names = set(archive.getnames())
            self.assertIn('private-data/case.txt', names)
            self.assertNotIn('.env', names)
        finally:
            os.remove(archive_path)

    def test_rejects_path_outside_project(self):
        (self.project / '.cluster-ci-local-include').write_text('../outside\n')
        with self.assertRaises(SystemExit):
            package_local_source(self.project)


if __name__ == '__main__':
    unittest.main()
