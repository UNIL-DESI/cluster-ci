"""
Unit tests for Chantier H6 (Host Guard - Culprit Container Selection and Targeted Kill).
Verifies:
- Selection of culprit container based on per-container memory usage (RAM + VRAM)
- Protection of innocent containers when host reserve or hard limit is breached
- Correct generation of marker file and docker kill targeted specifically at culprit
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.runner.host_guard import (
    get_container_memory_usage,
    select_culprit_container,
    kill_culprit_container,
)


class TestHostGuardCulprit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_select_culprit_highest_ram(self):
        """Vérifie la sélection du conteneur consommant le plus de RAM parmi plusieurs candidats."""
        candidates = ["cluster-job-job1-runner1", "cluster-job-job2-culprit", "cluster-job-job3-runner3"]
        usage_map = {
            "cluster-job-job1-runner1": {"ram_bytes": 4 * 1024**3, "vram_bytes": 0, "total_bytes": 4 * 1024**3},
            "cluster-job-job2-culprit": {"ram_bytes": 22 * 1024**3, "vram_bytes": 0, "total_bytes": 22 * 1024**3},
            "cluster-job-job3-runner3": {"ram_bytes": 8 * 1024**3, "vram_bytes": 0, "total_bytes": 8 * 1024**3},
        }
        culprit = select_culprit_container(
            candidates=candidates,
            default_container="cluster-job-job1-runner1",
            memory_provider=lambda c: usage_map.get(c, {}),
        )
        self.assertEqual(culprit, "cluster-job-job2-culprit")

    def test_select_culprit_highest_vram(self):
        """Vérifie la sélection du conteneur avec la consommation VRAM prédominante."""
        candidates = ["cluster-job-cpu-heavy", "cluster-job-gpu-culprit"]
        usage_map = {
            "cluster-job-cpu-heavy": {"ram_bytes": 10 * 1024**3, "vram_bytes": 0, "total_bytes": 10 * 1024**3},
            "cluster-job-gpu-culprit": {"ram_bytes": 2 * 1024**3, "vram_bytes": 20 * 1024**3, "total_bytes": 22 * 1024**3},
        }
        culprit = select_culprit_container(
            candidates=candidates,
            default_container="cluster-job-cpu-heavy",
            memory_provider=lambda c: usage_map.get(c, {}),
        )
        self.assertEqual(culprit, "cluster-job-gpu-culprit")

    def test_select_culprit_fallback_on_single_or_empty(self):
        """Vérifie le comportement nominal pour 1 seul candidat ou liste vide."""
        self.assertEqual(select_culprit_container(["single-c"]), "single-c")
        self.assertEqual(select_culprit_container([], default_container="fallback-c"), "fallback-c")

    def test_get_container_memory_usage_cgroup(self):
        """Vérifie la lecture des métriques cgroup synthétiques."""
        cg_root = Path(self.temp_dir) / "cgroup"
        cid = "abcdef123456"
        scope_dir = cg_root / "system.slice" / f"docker-{cid}.scope"
        scope_dir.mkdir(parents=True, exist_ok=True)
        (scope_dir / "memory.current").write_text("16106127360\n")  # ~15 GiB

        with patch("subprocess.run") as mock_sub:
            # Mock docker inspect
            mock_sub.return_value = MagicMock(returncode=0, stdout=f"{cid}|1234|true\n")
            res = get_container_memory_usage("test-container", cgroup_fs_root=cg_root)
            self.assertEqual(res["container"], "test-container")
            self.assertEqual(res["ram_bytes"], 16106127360)
            self.assertEqual(res["total_bytes"], 16106127360)

    def test_kill_culprit_container_writes_marker_and_kills_target(self):
        """Vérifie l'écriture du fichier marker et l'appel docker kill sur le conteneur fautif."""
        marker_path = Path(self.temp_dir) / "host_guard_killed.marker"
        culprit = "cluster-job-job2-culprit"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            ok = kill_culprit_container(
                culprit_container=culprit,
                reason="HARD LIMIT BREACHED: MemAvailable < reserve",
                marker_file=marker_path,
                used_gb=28.5,
                available_gb=8.2,
                reserve_gb=12.0,
            )
            self.assertTrue(ok)
            mock_run.assert_called_once_with(["docker", "kill", culprit], capture_output=True, check=False)

        # Vérifier le contenu du marker file
        self.assertTrue(marker_path.is_file())
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "killed")
        self.assertEqual(data["container"], culprit)
        self.assertEqual(data["used_gb"], "28.5")
        self.assertEqual(data["available_gb"], "8.2")
        self.assertEqual(data["exit_code"], 137)

        # Vérifier le marker dédié au conteneur dans tempdir
        tmp_marker = Path(tempfile.gettempdir()) / f"host_guard_{culprit}.marker"
        self.assertTrue(tmp_marker.is_file())
        tmp_data = json.loads(tmp_marker.read_text(encoding="utf-8"))
        self.assertEqual(tmp_data["container"], culprit)
        tmp_marker.unlink()


if __name__ == "__main__":
    unittest.main()
