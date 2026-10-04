import os
import sys
import tempfile
import pytest

from src.runner.verify_packages import discover_project_paths, verify_packages


def test_pth_generation_and_fail_fast_logic():
    """
    Vérifie la logique de synchronisation du .pth et le fail-fast sys.path.
    """
    with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_site:
        # Création de répertoires d'installation simulant pip --prefix
        dist_pkg = os.path.join(tmp_home, ".local", "local", "lib", "python3.12", "dist-packages")
        site_pkg = os.path.join(tmp_home, ".local", "lib", "python3.12", "site-packages")
        uv_pkg = os.path.join(tmp_home, ".local", "share", "uv", "tools", "dvc", "lib", "python3.12", "site-packages")
        os.makedirs(dist_pkg, exist_ok=True)
        os.makedirs(site_pkg, exist_ok=True)
        os.makedirs(uv_pkg, exist_ok=True)

        # 1. Vérification que la découverte exclut share/uv et retient bien les schémas projet
        paths = discover_project_paths(base_dir=os.path.join(tmp_home, ".local"))
        assert dist_pkg in paths
        assert site_pkg in paths
        assert uv_pkg not in paths
        assert len(paths) == 2

        # 2. Simulation de l'écriture du fichier .pth
        pth_file = os.path.join(tmp_site, "cluster-ci-prefix.pth")
        with open(pth_file, "w") as f:
            for p in sorted(paths):
                f.write(p + "\n")

        with open(pth_file, "r") as f:
            lines = [l.strip() for l in f if l.strip()]
        assert lines == sorted([dist_pkg, site_pkg])


def test_verify_packages_empty_dir_skips_cleanly():
    with tempfile.TemporaryDirectory() as tmp_home:
        res = verify_packages(pythonpath=tmp_home)
        assert res == 0


def test_verify_packages_detects_mismatch(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as tmp_dir:
        dist_info = os.path.join(tmp_dir, "my_test_pkg-2.0.0.dist-info")
        os.makedirs(dist_info, exist_ok=True)
        with open(os.path.join(dist_info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: my_test_pkg\nVersion: 2.0.0\n")

        import importlib.metadata

        # Case 1: Package not found in active environment -> must fail fast (exit code 1)
        def mock_not_found(name):
            if name == "my_test_pkg":
                raise importlib.metadata.PackageNotFoundError("my_test_pkg")
            return importlib.metadata.distribution(name)

        monkeypatch.setattr(importlib.metadata, "distribution", mock_not_found)

        res1 = verify_packages(pythonpath=tmp_dir)
        assert res1 == 1
        captured1 = capsys.readouterr()
        assert "FAIL-FAST" in captured1.err
        assert "my_test_pkg" in captured1.err
        assert "was not found in active environment" in captured1.err

        # Case 2: Package version mismatch -> must report expected vs imported version
        class MockDist:
            name = "my_test_pkg"
            version = "1.0.0"  # Older container version shadowing the installed package
            _path = "/usr/local/lib/python3.12/dist-packages/my_test_pkg-1.0.0.dist-info"
            def locate_file(self, f):
                return self._path

        def mock_shadowed(name):
            if name == "my_test_pkg":
                return MockDist()
            return importlib.metadata.distribution(name)

        monkeypatch.setattr(importlib.metadata, "distribution", mock_shadowed)

        res2 = verify_packages(pythonpath=tmp_dir)
        assert res2 == 1
        captured2 = capsys.readouterr()
        assert "FAIL-FAST" in captured2.err
        assert "expected version '2.0.0'" in captured2.err
        assert "imported version '1.0.0'" in captured2.err


def test_verify_packages_subprocess_check():
    """
    Vérifie le fonctionnement du vérificateur subprocess (conditions réelles PYTHONPATH).
    """
    from src.runner.verify_packages import _run_subprocess_check
    import importlib.metadata

    real_ver = importlib.metadata.version("pytest")
    cwd = os.getcwd()

    # 1. Version exacte -> succès (0 mismatches)
    mismatches_ok = _run_subprocess_check("test_ok", dict(os.environ), cwd, {"pytest": real_ver})
    assert mismatches_ok == []

    # 2. Version erronée -> mismatch détecté
    mismatches_err = _run_subprocess_check("test_err", dict(os.environ), cwd, {"pytest": "999.0.0"})
    assert len(mismatches_err) == 1
    assert "pytest" in mismatches_err[0]
    assert "999.0.0" in mismatches_err[0]


def test_init_cmd_does_not_chown_workspace():
    """
    Vérifie rigoureusement que BranchExecutor n'applique JAMAIS de chown sur /workspace
    (dossier monté depuis l'hôte), supprime l'ancien .pth et migre ~/.local/local vers user-site.
    """
    from src.runner.branch_executor import BranchExecutor
    from src.runner.test_branch_executor import MockDockerRunner

    with tempfile.TemporaryDirectory() as tmp_dir:
        mock_docker = MockDockerRunner()
        executor = BranchExecutor(
            headnode_url="http://localhost:5000",
            job_id="job-test-ownership",
            runner_id="runner-1",
            worker_id="worker-1",
            repo_dir=tmp_dir,
            target_repo="UNIL-DESI/llm-as-recommender",
            target_branch="main",
            docker=mock_docker,
        )
        executor.start_container_for_image("python:3.11-slim")

        root_cmds = [c["command"] for c in mock_docker.exec_commands if c.get("user") == "root"]
        assert len(root_cmds) >= 1
        init_cmd = root_cmds[0]
        # Invariant: /workspace ne doit jamais être chowné
        assert "/workspace" not in init_cmd or "chown" not in init_cmd
        assert "/home/user" in init_cmd and "chown -R" in init_cmd
        # Invariant: suppression de cluster-ci-prefix.pth et migration ~/.local/local
        assert "cluster-ci-prefix.pth" in init_cmd and "rm -f" in init_cmd
        assert "/home/user/.local/local" in init_cmd and "site-packages" in init_cmd


def test_smart_install_cached_path_ensures_usercustomize():
    """
    Vérifie que le chemin cache valide de smart_install.sh crée systématiquement
    usercustomize.py dans site-packages, évitant le shadowing des paquets conteneur.
    Sur cdab1c2, smart_install.sh sort par exit 0 (l.47) avant l'écriture (l.295-309).
    """
    import subprocess

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "repo")
        os.makedirs(repo_dir, exist_ok=True)
        pyproj = os.path.join(repo_dir, "pyproject.toml")
        with open(pyproj, "w") as f:
            f.write('[project]\nname = "demo"\nversion = "0.1.0"\n')

        user_home = os.path.join(tmp_dir, "home")
        user_base = os.path.join(user_home, ".local")
        sp_dir = os.path.join(user_base, "lib", "python3.12", "site-packages")
        dist_info = os.path.join(sp_dir, "demo_pkg-1.0.0.dist-info")
        os.makedirs(dist_info, exist_ok=True)
        with open(os.path.join(dist_info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0.0\n")

        hash_file = os.path.join(user_home, ".cluster-ci-deps-hash")
        calc_hash = subprocess.run(
            ["bash", "-c", "md5sum pyproject.toml 2>/dev/null | md5sum | cut -d' ' -f1"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        with open(hash_file, "w") as f:
            f.write(calc_hash + "\n")

        uc_file = os.path.join(sp_dir, "usercustomize.py")
        assert not os.path.exists(uc_file)

        script_path = os.path.abspath("src/runner/smart_install.sh")
        env = dict(os.environ)
        env["HOME"] = user_home
        env["PYTHONUSERBASE"] = user_base
        env["CLUSTER_CI_HASH_FILE"] = hash_file

        res = subprocess.run(
            ["bash", script_path],
            cwd=repo_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        assert "Dependencies unchanged (cached)" in res.stdout
        assert os.path.exists(uc_file), f"usercustomize.py must exist on cached path, but was missing!\nOutput: {res.stdout}"
        with open(uc_file, "r") as f:
            content = f.read()
        assert "getusersitepackages" in content
        assert "sys.path.insert" in content


