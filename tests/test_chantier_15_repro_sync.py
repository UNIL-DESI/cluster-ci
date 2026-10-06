import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runner.dvc_git_helper import format_auto_sync_commit_message
from src.runner import dvc_iterative_repro as runner


class TestChantier15AutoSyncCommitMessage(unittest.TestCase):
    def test_commit_message_formats(self):
        """Vérifie la fidélité des messages de commit générés selon les types de fichiers stagés (Issue #106)."""
        # Seul dvc.lock
        self.assertEqual(
            format_auto_sync_commit_message(["dvc.lock"]),
            "chore(ci): auto-sync dvc.lock [skip ci]"
        )

        # Seul metrics
        self.assertEqual(
            format_auto_sync_commit_message(["metrics.json"]),
            "chore(ci): auto-sync metrics [skip ci]"
        )

        # Seul artifacts
        self.assertEqual(
            format_auto_sync_commit_message(["artifacts/model.pt"]),
            "chore(ci): auto-sync artifacts [skip ci]"
        )

        # Metrics et dvc.lock
        self.assertEqual(
            format_auto_sync_commit_message(["metrics.json", "dvc.lock"]),
            "chore(ci): auto-sync metrics and dvc.lock [skip ci]"
        )

        # Artifacts et dvc.lock
        self.assertEqual(
            format_auto_sync_commit_message(["artifacts/model.onnx", "dvc.lock"]),
            "chore(ci): auto-sync artifacts and dvc.lock [skip ci]"
        )

        # Metrics, artifacts et dvc.lock
        self.assertEqual(
            format_auto_sync_commit_message(["metrics.json", "artifacts/weights.bin", "dvc.lock"]),
            "chore(ci): auto-sync metrics, artifacts and dvc.lock [skip ci]"
        )

        # Code ou autres fichiers
        self.assertEqual(
            format_auto_sync_commit_message(["src/train.py"]),
            "chore(ci): auto-sync changes [skip ci]"
        )

        # Liste vide
        self.assertEqual(
            format_auto_sync_commit_message([]),
            "chore(ci): auto-sync changes [skip ci]"
        )


class TestChantier15IterativeRepro(unittest.TestCase):
    def test_regular_stage_includes_dash_s(self):
        """Test qu'un stage régulier (ex: train) reçoit bien -s automatiquement."""
        with patch.dict(os.environ, {"IS_LOCAL": "1"}, clear=True), \
                patch.object(runner, "get_dvc_dag", return_value=["train"]), \
                patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            runner.main()

        commands = [call[0][0] for call in run.call_args_list]
        self.assertTrue(any(cmd[:3] == ["dvc", "repro", "train"] and "-s" in cmd for cmd in commands))

    def test_dvc_code_analysis_preserves_exclusion_from_dash_s(self):
        """Test que dvc-code-analysis n'a pas -s forcé (exclusion intentionnelle du commit fe381b43)."""
        with patch.dict(os.environ, {"IS_LOCAL": "1"}, clear=True), \
                patch.object(runner, "get_dvc_dag", return_value=["dvc-code-analysis"]), \
                patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            runner.main()

        commands = [call[0][0] for call in run.call_args_list]
        self.assertTrue(any(cmd[:3] == ["dvc", "repro", "dvc-code-analysis"] and "-s" not in cmd for cmd in commands))


if __name__ == "__main__":
    unittest.main()
