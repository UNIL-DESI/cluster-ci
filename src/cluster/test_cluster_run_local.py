import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.cluster.cluster_run import local_job_branch, package_local_source


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

    def test_local_label_branch_is_backward_compatible(self):
        self.assertEqual(local_job_branch('alice'), 'local-draft/alice')
        self.assertEqual(
            local_job_branch('alice', 'summary-b'),
            'local-draft/alice/summary-b',
        )

    def test_standalone_cli_accepts_label_only_with_local_mode(self):
        script = Path(__file__).with_name('cluster_run.py')
        help_result = subprocess.run(
            [sys.executable, str(script), '--help'],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('--label', help_result.stdout)

        invalid_mode = subprocess.run(
            [sys.executable, str(script), '--label', 'summary-a'],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        self.assertEqual(invalid_mode.returncode, 2)
        self.assertIn('--label can only be used with --local', invalid_mode.stderr)

        invalid_label = subprocess.run(
            [sys.executable, str(script), '--local', '--label', 'bad/label'],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        self.assertEqual(invalid_label.returncode, 2)
        self.assertIn('Local job labels must be', invalid_label.stderr)


if __name__ == '__main__':
    unittest.main()
