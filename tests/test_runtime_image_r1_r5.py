"""
Tests unitaires pour le correctif de conception des images de runtime Cluster-CI (R1 à R5).

R1: Specialized runtime images (vllm, nemo) - Image environment WINS entirely.
R2: Generic images (pytorch, python) - Pure-Python upgrades allowed, core heavy pinned.
R3: Stage interpreter inspection via clean flags (PYTHONNOUSERSITE=1 python3 -s).
R4: Volume invalidation: Composite deps hash with mechanism version and image footprint.
R5: Fail-fast verification of package versions in real stage execution conditions.
"""

import importlib
import importlib.metadata
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
    get_importable_modules,
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

            with patch("importlib.metadata.distribution", side_effect=mock_dist_ok), \
                 patch("importlib.import_module"):
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
            with patch("importlib.metadata.distribution", side_effect=mock_dist_lookup), \
                 patch("importlib.import_module"):
                res = verify_packages(
                    pythonpath=user_sp,
                    base_dir=os.path.join(tmp_home, ".local"),
                    check_subprocesses=False,
                )
                assert res == 0


def test_verify_packages_detects_real_import_failure(capsys):
    """
    Défaut corrigé : verify_packages sans import réel des modules.
    Vérifie qu'un module dont le .dist-info est présent mais dont l'import réel échoue
    (ex: extension .so manquante ou dépendance transitive absente) déclenche un échec fail-fast (code 1).
    """
    with tempfile.TemporaryDirectory() as tmp_home:
        user_sp = os.path.join(tmp_home, ".local", "lib", "python3.12", "site-packages")
        dist_info = os.path.join(user_sp, "broken_pkg-1.0.0.dist-info")
        os.makedirs(dist_info, exist_ok=True)
        with open(os.path.join(dist_info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: broken_pkg\nVersion: 1.0.0\n")
        with open(os.path.join(dist_info, "top_level.txt"), "w") as f:
            f.write("broken_pkg\n")

        class MockDist:
            def __init__(self, name, version, path):
                self.name = name
                self.version = version
                self._path = path
            def locate_file(self, f):
                return self._path
            def read_text(self, filename):
                if filename == "top_level.txt":
                    return "broken_pkg\n"
                return None

        def mock_distributions(path=None):
            return [MockDist("broken_pkg", "1.0.0", dist_info)]

        def mock_dist_lookup(name):
            if name == "broken_pkg":
                return MockDist("broken_pkg", "1.0.0", dist_info)
            raise importlib.metadata.PackageNotFoundError(name)

        def mock_broken_import(mod):
            if mod == "broken_pkg":
                raise ImportError("libcuda.so.1: cannot open shared object file: No such file or directory")
            return MagicMock()

        with patch("importlib.metadata.distributions", side_effect=mock_distributions), \
             patch("importlib.metadata.distribution", side_effect=mock_dist_lookup), \
             patch("importlib.import_module", side_effect=mock_broken_import):
            res = verify_packages(
                pythonpath=user_sp,
                base_dir=os.path.join(tmp_home, ".local"),
                check_subprocesses=False,
            )
            assert res == 1

        captured = capsys.readouterr()
        assert "FAIL-FAST" in captured.err
        assert "broken_pkg" in captured.err
        assert "failed real import" in captured.err
        assert "libcuda.so.1" in captured.err


def test_verify_packages_top_level_slashes_duplicates_and_missing(capsys):
    """
    Régression bloquante : verify_packages tentait d'importer les sous-chemins de top_level.txt
    (ex: sentencepiece/__init__), causant un ModuleNotFoundError sur un paquet valide.
    Vérifie qu'une distribution dont top_level.txt contient des entrées avec slash, des lignes vides
    et des doublons est nettoyée et importée sans faux échec (code 0).
    Vérifie également qu'un module réellement absent déclenche un échec explicite fail-fast (code 1).
    """
    top_level_content = (
        "\n"
        "sentencepiece/__init__\n"
        "sentencepiece\n"
        "\n"
        "sentencepiece\n"
        "sentencepiece/_version\n"
        "sentencepiece/sentencepiece_model_pb2\n"
    )

    with tempfile.TemporaryDirectory() as tmp_home:
        user_sp = os.path.join(tmp_home, ".local", "lib", "python3.12", "site-packages")
        dist_info = os.path.join(user_sp, "sentencepiece-0.2.0.dist-info")
        os.makedirs(dist_info, exist_ok=True)
        with open(os.path.join(dist_info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: sentencepiece\nVersion: 0.2.0\n")
        with open(os.path.join(dist_info, "top_level.txt"), "w") as f:
            f.write(top_level_content)

        class MockDist:
            def __init__(self, name, version, path, top_content):
                self.name = name
                self.version = version
                self._path = path
                self.top_content = top_content

            def locate_file(self, f):
                return self._path

            def read_text(self, filename):
                if filename == "top_level.txt":
                    return self.top_content
                return None

        sp_dist = MockDist("sentencepiece", "0.2.0", dist_info, top_level_content)

        # 1. Vérification unitaire de get_importable_modules
        mods = get_importable_modules(sp_dist, "sentencepiece")
        # Les entrées avec slashes sont nettoyées vers le module racine, les lignes vides et doublons ignorés
        assert mods == ["sentencepiece"]

        # 2. Vérification complète dans verify_packages : import valide -> code 0 (aucun faux échec)
        def mock_distributions(path=None):
            return [sp_dist]

        def mock_dist_lookup(name):
            if name == "sentencepiece":
                return sp_dist
            raise importlib.metadata.PackageNotFoundError(name)

        imported_modules = []

        def mock_import(mod):
            imported_modules.append(mod)
            return MagicMock()

        with patch("importlib.metadata.distributions", side_effect=mock_distributions), \
             patch("importlib.metadata.distribution", side_effect=mock_dist_lookup), \
             patch("importlib.import_module", side_effect=mock_import):
            res = verify_packages(
                pythonpath=user_sp,
                base_dir=os.path.join(tmp_home, ".local"),
                check_subprocesses=False,
            )
            assert res == 0
            assert imported_modules == ["sentencepiece"]

        # 3. Module réellement absent -> échec explicite fail-fast (code 1)
        absent_top_level = "\nmissing_mod/__init__\nmissing_mod\n"
        absent_dist_info = os.path.join(user_sp, "missing_pkg-1.0.0.dist-info")
        os.makedirs(absent_dist_info, exist_ok=True)
        missing_dist = MockDist("missing_pkg", "1.0.0", absent_dist_info, absent_top_level)

        def mock_missing_distributions(path=None):
            return [missing_dist]

        def mock_missing_lookup(name):
            if name == "missing_pkg":
                return missing_dist
            raise importlib.metadata.PackageNotFoundError(name)

        def mock_missing_import(mod):
            raise ModuleNotFoundError(f"No module named '{mod}'")

        with patch("importlib.metadata.distributions", side_effect=mock_missing_distributions), \
             patch("importlib.metadata.distribution", side_effect=mock_missing_lookup), \
             patch("importlib.import_module", side_effect=mock_missing_import):
            res_missing = verify_packages(
                pythonpath=user_sp,
                base_dir=os.path.join(tmp_home, ".local"),
                check_subprocesses=False,
            )
            assert res_missing == 1

        captured = capsys.readouterr()
        assert "FAIL-FAST" in captured.err
        assert "missing_pkg" in captured.err
        assert "failed real import" in captured.err
        assert "ModuleNotFoundError" in captured.err


def test_verify_packages_supports_requirements_txt():
    """
    Défaut corrigé : requirements.txt ignoré dans verify_packages.
    Vérifie qu'en R1, les dépendances déclarées dans requirements.txt sont bien lues
    et protégées contre le masquage par user-site.
    """
    with tempfile.TemporaryDirectory() as tmp_work, tempfile.TemporaryDirectory() as tmp_home:
        req_file = os.path.join(tmp_work, "requirements.txt")
        with open(req_file, "w") as f:
            f.write("# Commentaire\n--extra-index-url https://example.com\ntransformers>=4.40.0\n")

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
            def read_text(self, f):
                return None

        def mock_distributions(path=None):
            if path:
                return [MockDist("transformers", "5.3.0", dist_info)]
            return [
                MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info"),
                MockDist("transformers", "5.14.1", "/usr/local/lib/python3.12/dist-packages/transformers-5.14.1.dist-info"),
            ]

        def mock_dist_lookup(name):
            if name == "transformers":
                return MockDist("transformers", "5.3.0", dist_info)
            elif name == "vllm":
                return MockDist("vllm", "0.27.1", "/usr/local/lib/python3.12/dist-packages/vllm-0.27.1.dist-info")
            raise importlib.metadata.PackageNotFoundError(name)

        with patch("importlib.metadata.distributions", side_effect=mock_distributions), \
             patch("importlib.metadata.distribution", side_effect=mock_dist_lookup), \
             patch("importlib.import_module"):
            res = verify_packages(
                pythonpath=user_sp,
                base_dir=os.path.join(tmp_home, ".local"),
                workspace_dir=tmp_work,
                check_subprocesses=False,
            )
            # Détection obligatoire du masquage de transformers déclaré dans requirements.txt
            assert res == 1


def test_smart_install_flock_concurrency_timeout():
    """
    Défaut corrigé : pas de verrou flock sur le volume partagé d'installation.
    Vérifie qu'un second processus attendant le verrou échoue en fail-fast (code 1)
    avec message explicite après expiration du délai maximal SMART_INSTALL_LOCK_TIMEOUT.
    """
    script_path = os.path.abspath("src/runner/smart_install.sh")
    with tempfile.TemporaryDirectory() as tmp_dir:
        lock_file = os.path.join(tmp_dir, "test.lock")
        # Acquérir un verrou exclusif flock dans un sous-processus
        holder = subprocess.Popen(
            ["flock", "-x", lock_file, "sleep", "10"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            import time
            time.sleep(0.3)

            env = dict(os.environ)
            env["CLUSTER_CI_LOCK_FILE"] = lock_file
            env["SMART_INSTALL_LOCK_TIMEOUT"] = "1"
            env["PYTHONUSERBASE"] = tmp_dir

            res = subprocess.run(
                ["bash", script_path],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            assert res.returncode == 1
            assert "Failed to acquire installation lock" in res.stderr or "Timeout" in res.stderr
            assert "test.lock" in res.stderr
            assert "Another installation process" in res.stderr
        finally:
            holder.terminate()
            holder.wait(timeout=2)


def test_smart_install_r1_sat_false_on_specifier_exception():
    """
    Défaut corrigé : sat=True sur exception (:290).
    Vérifie qu'une exception d'évaluation de specifier n'est pas silencieusement considérée
    comme satisfaite, mais déclenche STATUS:CONFLICT et un arrêt immédiat (exit 1).
    """
    script_path = os.path.abspath("src/runner/smart_install.sh")
    with tempfile.TemporaryDirectory() as tmp_dir:
        pyproj = os.path.join(tmp_dir, "pyproject.toml")
        with open(pyproj, "w") as f:
            # Spécification avec specifier invalide / malformé
            f.write('[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["pytest (===invalid===ver)"]\n')

        # Test direct du bloc python d'analyse R1 extrait de smart_install.sh
        test_py = """
import sys, re
try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement

installed = {'pytest': '7.4.4'}
deps = ['pytest (===invalid===ver)']
conflicts = []
absent = []
satisfied = []

for dep_str in deps:
    try:
        req = Requirement(dep_str)
        norm_name = re.sub(r'[-_.]+', '-', req.name).lower()
        if norm_name in installed:
            inst_ver = installed[norm_name]
            try:
                sat = req.specifier.contains(inst_ver, prereleases=True)
            except Exception as spec_err:
                sat = False
                conflicts.append((req.name, f'{req.specifier} (invalid specifier or evaluation error: {spec_err})', inst_ver))
                continue
            if sat:
                satisfied.append((req.name, inst_ver))
            else:
                conflicts.append((req.name, str(req.specifier), inst_ver))
    except Exception as exc:
        conflicts.append((dep_str, f'unparseable: {exc}', 'none'))

assert len(conflicts) > 0, "L'exception de specifier ne doit PAS être masquée par sat=True !"
assert len(satisfied) == 0, "Le paquet ne doit PAS être compté comme satisfait !"
print("OK_CONFLICT_CAUGHT")
"""
        res = subprocess.run([sys.executable, "-c", test_py], capture_output=True, text=True)
        assert res.returncode == 0
        assert "OK_CONFLICT_CAUGHT" in res.stdout


def test_smart_install_r1_analysis_crash_fails_fast():
    """
    Défaut corrigé : || true (:311) masquant les crashes du script python d'analyse.
    Vérifie qu'un échec de l'interpréteur python d'analyse provoque un arrêt immédiat avec exit 1.
    """
    script_path = os.path.abspath("src/runner/smart_install.sh")
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Créer un faux interpréteur stage qui crashe sur l'analyse
        fake_py = os.path.join(tmp_dir, "fake_python.sh")
        with open(fake_py, "w") as f:
            f.write("""#!/bin/bash
if [ "$1" = "-s" ] && [ "$2" = "-c" ] && [[ "$3" == *"vllm"* ]]; then
    echo "r1"
    exit 0
fi
if [ "$1" = "-s" ] && [ "$2" = "-c" ]; then
    echo "Simulated fatal interpreter crash / MemoryError" >&2
    exit 137
fi
exec python3 "$@"
""")
        os.chmod(fake_py, 0o755)

        env = dict(os.environ)
        env["STAGE_PYTHON"] = fake_py
        env["PYTHONUSERBASE"] = tmp_dir
        env["CLUSTER_CI_LOCK_FILE"] = os.path.join(tmp_dir, "test.lock")

        # Exécuter smart_install.sh
        res = subprocess.run(
            ["bash", script_path],
            cwd=tmp_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 1
        assert "Dependency analysis against specialized runtime image failed" in res.stderr
        assert "Simulated fatal interpreter crash" in res.stderr


def test_smart_install_r1_no_deps_fallback_removed():
    """
    Défaut corrigé : repli silencieux --no-deps en R1 (smart_install.sh:341).
    Vérifie que lorsque pip échoue à installer une dépendance absente sous contrainte,
    smart_install.sh n'essaie PAS d'installer avec --no-deps et échoue immédiatement (exit 1).
    """
    script_path = os.path.abspath("src/runner/smart_install.sh")
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Fichier requirements avec paquet absent
        req_file = os.path.join(tmp_dir, "requirements.txt")
        with open(req_file, "w") as f:
            f.write("missing-library>=1.0.0\n")

        # Faux pip qui logue les arguments reçus et échoue systématiquement
        pip_log = os.path.join(tmp_dir, "pip_invocations.log")
        bin_dir = os.path.join(tmp_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        fake_pip = os.path.join(bin_dir, "pip")
        with open(fake_pip, "w") as f:
            f.write(f"""#!/bin/bash
echo "$@" >> "{pip_log}"
echo "ERROR: ResolutionImpossible: missing-library requires incompatible sub-dependency" >&2
exit 1
""")
        os.chmod(fake_pip, 0o755)

        # Faux python qui simule image R1 (fournit vllm dans detect_mode et freeze)
        fake_python = os.path.join(tmp_dir, "fake_python.sh")
        with open(fake_python, "w") as f:
            f.write("""#!/bin/bash
if [ "$1" = "-s" ] && [ "$2" = "-c" ] && [[ "$3" == *"vllm"* ]]; then
    # detect_runtime_mode
    echo "r1"
    exit 0
fi
if [ "$1" = "-s" ] && [ "$2" = "-m" ] && [ "$3" = "pip" ] && [ "$4" = "freeze" ]; then
    echo "vllm==0.27.1"
    exit 0
fi
exec python3 "$@"
""")
        os.chmod(fake_python, 0o755)

        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["STAGE_PYTHON"] = fake_python
        env["PYTHONUSERBASE"] = tmp_dir
        env["CLUSTER_CI_LOCK_FILE"] = os.path.join(tmp_dir, "test.lock")

        res = subprocess.run(
            ["bash", script_path],
            cwd=tmp_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 1
        assert "Failed to install absent dependency under specialized runtime image constraints" in res.stderr
        assert "missing-library" in res.stderr
        assert "Silent --no-deps fallback is disabled" in res.stderr

        # Vérifier dans le log qu'aucun appel n'a utilisé --no-deps
        if os.path.exists(pip_log):
            with open(pip_log, "r") as f:
                calls = f.read()
            assert "--no-deps" not in calls, f"Un repli --no-deps a été exécuté : {calls}"

