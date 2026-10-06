import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runner.dvc_git_helper import (
    _get_git_env,
    push_with_retries,
)


class TestChantier14GitHelper(unittest.TestCase):
    def test_get_git_env_forces_c_locale(self):
        """Test que _get_git_env() force LC_ALL=C et LANG=C même avec une locale française."""
        with patch.dict(os.environ, {"LANG": "fr_FR.UTF-8", "LC_ALL": "fr_FR.UTF-8"}):
            env = _get_git_env()
            self.assertEqual(env.get("LC_ALL"), "C")
            self.assertEqual(env.get("LANG"), "C")

    def test_autostash_conflict_includes_stash_ref_and_no_abort_when_not_rebasing(self):
        """Test que le conflit autostash lève RuntimeError avec stash@{0} et n'appelle pas rebase --abort sans rebase actif."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dot_git = Path(temp_dir) / ".git"
            dot_git.mkdir(parents=True, exist_ok=True)

            def mock_run(cmd, *args, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                res = MagicMock()
                if "push" in cmd_str and "origin" in cmd_str:
                    res.returncode = 1
                    res.stderr = "[rejected] (fetch first)"
                    res.stdout = ""
                elif "pull" in cmd_str and "--rebase" in cmd_str:
                    res.returncode = 1
                    res.stderr = "error: could not detach HEAD\nApplying autostash resulted in conflicts."
                    res.stdout = ""
                elif "rev-parse" in cmd_str and "--git-dir" in cmd_str:
                    res.returncode = 0
                    res.stdout = str(dot_git) + "\n"
                elif "stash" in cmd_str and "list" in cmd_str:
                    res.returncode = 0
                    res.stdout = "stash@{0}: WIP on main: 1234567 commit\n"
                elif "rebase" in cmd_str and "--abort" in cmd_str:
                    self.fail("git rebase --abort ne devrait pas être appelé si rebase-merge ou rebase-apply n'existe pas")
                else:
                    res.returncode = 0
                    res.stdout = ""
                return res

            with patch("subprocess.run", side_effect=mock_run):
                with self.assertRaises(RuntimeError) as ctx:
                    push_with_retries(current_branch="main", cwd=temp_dir)

                self.assertIn("stash@{0}", str(ctx.exception))
                self.assertIn("Applying autostash resulted in conflicts", str(ctx.exception))

    def test_autostash_conflict_aborts_when_rebase_active(self):
        """Test que git rebase --abort EST appelé si rebase-merge est présent lors d'un échec."""
        with tempfile.TemporaryDirectory() as temp_dir:
            dot_git = Path(temp_dir) / ".git"
            dot_git.mkdir(parents=True, exist_ok=True)
            rebase_merge_dir = dot_git / "rebase-merge"
            rebase_merge_dir.mkdir(parents=True, exist_ok=True)

            rebase_abort_called = []

            def mock_run(cmd, *args, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                res = MagicMock()
                if "push" in cmd_str and "origin" in cmd_str:
                    res.returncode = 1
                    res.stderr = "[rejected] (fetch first)"
                    res.stdout = ""
                elif "pull" in cmd_str and "--rebase" in cmd_str:
                    res.returncode = 1
                    res.stderr = "Failed to merge in the changes."
                    res.stdout = ""
                elif "rev-parse" in cmd_str and "--git-dir" in cmd_str:
                    res.returncode = 0
                    res.stdout = str(dot_git) + "\n"
                elif "rebase" in cmd_str and "--abort" in cmd_str:
                    rebase_abort_called.append(True)
                    res.returncode = 0
                    res.stdout = ""
                else:
                    res.returncode = 0
                    res.stdout = ""
                return res

            with patch("subprocess.run", side_effect=mock_run):
                with self.assertRaises(RuntimeError):
                    push_with_retries(current_branch="main", cwd=temp_dir)

                self.assertTrue(rebase_abort_called, "git rebase --abort aurait dû être appelé")

    def test_git_plumbing_failure_raises_runtime_error(self):
        """Test que l'échec de git rev-parse ou hash-object lève immédiatement RuntimeError lors du plumbing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            metric_file = Path(temp_dir) / "metrics.json"
            metric_file.write_text('{"loss": 0.5}')

            def mock_run(cmd, *args, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                res = MagicMock()
                if "rev-parse" in cmd_str and "^{tree}" in cmd_str:
                    res.returncode = 1
                    res.stderr = "fatal: Not a valid object name"
                elif "ls-remote" in cmd_str:
                    res.returncode = 0
                    res.stdout = "abc1234\trefs/heads/main\n"
                else:
                    res.returncode = 0
                    res.stdout = "abc1234\n"
                return res

            with patch("src.runner.dvc_git_helper.get_allowed_sync_paths", return_value={"metrics.json"}):
                with patch("subprocess.run", side_effect=mock_run):
                    with self.assertRaises(RuntimeError) as ctx:
                        push_with_retries(current_branch="main", files_to_commit=["metrics.json"], cwd=temp_dir)

                    self.assertIn("git rev-parse tree failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
