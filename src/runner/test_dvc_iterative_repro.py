import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.runner import dvc_iterative_repro as runner


class TestIterativeRepro(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_directory = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous_directory)
        self.enter_context = contextlib.ExitStack()
        self.addCleanup(self.enter_context.close)
        self.enter_context.enter_context(patch.object(sys, "argv", ["runner"]))
        self.enter_context.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_local_failure_keeps_partial_results_without_git_commands(self):
        partial = Path("metrics.json")
        partial.write_text('{"completed_steps": 2}\n')
        with patch.dict(os.environ, {"IS_LOCAL": "1"}, clear=True), \
                patch.object(runner, "get_dvc_dag", return_value=["train", "evaluate"]), \
                patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 23)) as run:
            with self.assertRaises(SystemExit) as raised:
                runner.main()

        self.assertEqual(raised.exception.code, 23)
        run.assert_called_once_with(["dvc", "repro", "train", "-s"])
        self.assertEqual(partial.read_text(), '{"completed_steps": 2}\n')
        self.assertFalse(Path(runner.ITERATIVE_STATUS_FILE).exists())

    def test_nonlocal_failure_preserves_git_sync(self):
        def command_result(command, **kwargs):
            if command[0] == "dvc":
                return subprocess.CompletedProcess(command, 23)
            return subprocess.CompletedProcess(command, 0, stdout=" M metrics.json\n")

        for local_mode in (None, "0"):
            environment = {"TARGET_BRANCH": "main"}
            if local_mode is not None:
                environment["IS_LOCAL"] = local_mode
            with self.subTest(local_mode=local_mode), \
                    patch.dict(os.environ, environment, clear=True), \
                    patch.object(runner, "get_dvc_dag", return_value=["train"]), \
                    patch.object(runner.subprocess, "run", side_effect=command_result) as run:
                with self.assertRaises(SystemExit) as raised:
                    runner.main()
                self.assertEqual(raised.exception.code, 23)
                commands = [call[0][0] for call in run.call_args_list]
                self.assertIn(["git", "add", "."], commands)
                self.assertIn(["git", "commit", "-m", "cluster-ci: failed stage train [skip ci]"], commands)
                self.assertIn(["git", "push", "origin", "main"], commands)
                self.assertFalse(Path(runner.ITERATIVE_STATUS_FILE).exists())

    def test_local_success_still_syncs_through_helper(self):
        with patch.dict(os.environ, {"IS_LOCAL": "1"}, clear=True), \
                patch.object(runner, "get_dvc_dag", return_value=["train"]), \
                patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            runner.main()

        commands = [call[0][0] for call in run.call_args_list]
        helper = str(Path(runner.__file__).resolve().with_name("dvc_git_helper.py"))
        self.assertEqual(commands, [
            ["dvc", "repro", "train", "-s"],
            ["uv", "run", "--with", "ruamel.yaml", "python3", helper, "sync"],
        ])
        self.assertFalse(Path(runner.ITERATIVE_STATUS_FILE).exists())


if __name__ == "__main__":
    unittest.main()
