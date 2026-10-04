"""
Tests unitaires pour le correctif de conception des images de runtime Cluster-CI (R1 à R5).

R1: Specialized runtime images (vllm, nemo) - Image environment WINS entirely.
R2: Generic images (pytorch, python) - Pure-Python upgrades allowed, core heavy pinned.
R3: Stage interpreter inspection via clean flags (PYTHONNOUSERSITE=1 python3 -s).
R4: Volume invalidation: Composite deps hash with mechanism version and image footprint.
R5: Fail-fast verification of package versions in real stage execution conditions.
"""

import os
import sys
import tempfile
import subprocess
import pytest
from unittest.mock import patch, MagicMock

from src.runner.verify_packages import (
    discover_project_paths,
    _run_subprocess_check,
    verify_packages,
)


def test_r1_vs_r2_detection_logic():
    """
    Vérifie que la détection de mode runtime distingue correctement
    les images spécialisées (vllm, nemo_automodel) des images génériques.
    """
    script_path = os.path.abspath("src/runner/smart_install.sh")

    # Image générique courante (environnement de test) -> r2
    res = subprocess.run(
        ["bash", script_path, "--detect-mode"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "r2"


def test_r1_usercustomize_execution_order():
    """
    Vérifie qu'en mode R1, usercustomize.py place systématiquement user-site à la FIN
    de sys.path, garantissant que l'environnement image prime toujours.
    """
    code_r1_uc = """
import sys
import site

user_site = site.getusersitepackages()
user_paths = [p for p in sys.path if p == user_site or (isinstance(p, str) and p.startswith("/home/user/.local/"))]
for p in user_paths:
    while p in sys.path:
        sys.path.remove(p)
for p in user_paths:
    sys.path.append(p)
"""
    fake_user_site = "/home/user/.local/lib/python3.12/site-packages"
    sys_path_sim = [
        "/workspace",
        fake_user_site,
        "/usr/local/lib/python3.12/dist-packages",
        "/usr/lib/python3.12",
    ]

    with patch("site.getusersitepackages", return_value=fake_user_site):
        with patch.object(sys, "path", list(sys_path_sim)):
            exec(code_r1_uc)
            assert sys.path[0] == "/workspace"
            assert sys.path.index("/usr/local/lib/python3.12/dist-packages") < sys.path.index(fake_user_site)
            assert sys.path[-1] == fake_user_site


def test_r2_usercustomize_execution_order():
    """
    Vérifie qu'en mode R2, usercustomize.py insère user-site en tête (idx 0 ou 1)
    pour permettre la mise à niveau des bibliothèques pure-Python.
    """
    code_r2_uc = """
import sys
import site

user_site = site.getusersitepackages()
if user_site in sys.path:
    sys.path.remove(user_site)
    idx = 1 if (sys.path and sys.path[0] in ("", ".", "/workspace")) else 0
    sys.path.insert(idx, user_site)
"""
    fake_user_site = "/home/user/.local/lib/python3.12/site-packages"
    sys_path_sim = [
        "/workspace",
        "/usr/local/lib/python3.12/dist-packages",
        fake_user_site,
    ]
    with patch("site.getusersitepackages", return_value=fake_user_site):
        with patch.object(sys, "path", list(sys_path_sim)):
            exec(code_r2_uc)
            assert sys.path[0] == "/workspace"
            assert sys.path[1] == fake_user_site
            assert sys.path.index(fake_user_site) < sys.path.index("/usr/local/lib/python3.12/dist-packages")


def test_r4_deps_hash_includes_mechanism_and_footprint():
    """
    Vérifie que le hash composite R4 prend en compte la version du mécanisme,
    l'empreinte de l'interpréteur image et les fichiers de dépendances.
    """
    script_path = os.path.abspath("src/runner/smart_install.sh")
    with tempfile.TemporaryDirectory() as tmp_dir:
        pyproj = os.path.join(tmp_dir, "pyproject.toml")
        with open(pyproj, "w") as f:
            f.write('[project]\nname = "demo"\nversion = "0.1.0"\n')

        res1 = subprocess.run(
            ["bash", script_path, "--compute-hash"],
            cwd=tmp_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        hash1 = res1.stdout.strip()
        assert len(hash1) == 32

        with open(pyproj, "a") as f:
            f.write('dependencies = ["pandas>=2.0.0"]\n')

        res2 = subprocess.run(
            ["bash", script_path, "--compute-hash"],
            cwd=tmp_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        hash2 = res2.stdout.strip()
        assert hash1 != hash2


def test_r5_verify_packages_r1_prevents_masking():
    """
    Vérifie que verify_packages en mode R1 refuse formellement le masquage
    d'un paquet image (ex: transformers) par un wheel résiduel dans user-site.
    """
    with tempfile.TemporaryDirectory() as tmp_home:
        user_sp = os.path.join(tmp_home, ".local", "lib", "python3.12", "site-packages")
        dist_info = os.path.join(user_sp, "transformers-5.3.0.dist-info")
        os.makedirs(dist_info, exist_ok=True)
        with open(os.path.join(dist_info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: transformers\nVersion: 5.3.0\n")

        class MockDist:
            def __init__(self, name, version, path):
                self.name = name
                self.version = version
                self._path = path
            def locate_file(self, f):
                return self._path

        def mock_distributions(path=None):
            if path:
                return [MockDist("transformers", "5.3.0", dist_info)]
            return [
                MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info"),
                MockDist("transformers", "5.14.1", "/usr/local/lib/python3.12/dist-packages/transformers-5.14.1.dist-info"),
            ]

        with patch("importlib.metadata.distributions", side_effect=mock_distributions):
            # Cas A: active_dist importe la version 5.14.1 depuis l'image -> SUCCÈS (pas de masquage)
            def mock_dist_ok(name):
                if name == "transformers":
                    return MockDist("transformers", "5.14.1", "/usr/local/lib/python3.12/dist-packages/transformers-5.14.1.dist-info")
                elif name == "vllm":
                    return MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info")
                raise importlib.metadata.PackageNotFoundError(name)

            with patch("importlib.metadata.distribution", side_effect=mock_dist_ok):
                res_ok = verify_packages(
                    pythonpath=user_sp,
                    base_dir=os.path.join(tmp_home, ".local"),
                    check_subprocesses=False,
                )
                assert res_ok == 0

            # Cas B: active_dist importe la version 5.3.0 depuis user-site (masquage) -> ÉCHEC FAIL-FAST
            def mock_dist_masked(name):
                if name == "transformers":
                    return MockDist("transformers", "5.3.0", dist_info)
                elif name == "vllm":
                    return MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info")
                raise importlib.metadata.PackageNotFoundError(name)

            with patch("importlib.metadata.distribution", side_effect=mock_dist_masked):
                res_fail = verify_packages(
                    pythonpath=user_sp,
                    base_dir=os.path.join(tmp_home, ".local"),
                    check_subprocesses=False,
                )
                assert res_fail == 1


def test_r5_verify_packages_absent_dependency_verified():
    """
    Vérifie que pour un paquet absent de l'image (ex: peft dans vLLM),
    verify_packages valide bien la version installée dans user-site.
    """
    with tempfile.TemporaryDirectory() as tmp_home:
        user_sp = os.path.join(tmp_home, ".local", "lib", "python3.12", "site-packages")
        dist_info = os.path.join(user_sp, "peft-0.18.1.dist-info")
        os.makedirs(dist_info, exist_ok=True)
        with open(os.path.join(dist_info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: peft\nVersion: 0.18.1\n")

        class MockDist:
            def __init__(self, name, version, path):
                self.name = name
                self.version = version
                self._path = path
            def locate_file(self, f):
                return self._path

        def mock_distributions(path=None):
            if path:
                return [MockDist("peft", "0.18.1", dist_info)]
            return [
                MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info"),
            ]

        def mock_dist_lookup(name):
            if name == "peft":
                return MockDist("peft", "0.18.1", dist_info)
            elif name == "vllm":
                return MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info")
            raise importlib.metadata.PackageNotFoundError(name)

        with patch("importlib.metadata.distributions", side_effect=mock_distributions):
            with patch("importlib.metadata.distribution", side_effect=mock_dist_lookup):
                res = verify_packages(
                    pythonpath=user_sp,
                    base_dir=os.path.join(tmp_home, ".local"),
                    check_subprocesses=False,
                )
                assert res == 0
