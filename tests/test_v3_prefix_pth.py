import os
import sys
import tempfile
import subprocess
import pytest


def test_pth_generation_and_fail_fast_logic():
    """
    Vérifie la logique de synchronisation du .pth et le fail-fast sys.path.
    """
    with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_site:
        # Création de répertoires d'installation simulant pip --prefix
        dist_pkg = os.path.join(tmp_home, ".local", "local", "lib", "python3.12", "dist-packages")
        site_pkg = os.path.join(tmp_home, ".local", "lib", "python3.12", "site-packages")
        os.makedirs(dist_pkg, exist_ok=True)
        os.makedirs(site_pkg, exist_ok=True)

        # 1. Vérification que la découverte trouve bien les 2 schémas
        cands = [
            os.path.join(r, d)
            for r, ds, _ in os.walk(tmp_home)
            for d in ds
            if d in ("site-packages", "dist-packages")
        ]
        assert len(cands) == 2
        assert dist_pkg in cands
        assert site_pkg in cands

        # 2. Simulation de l'écriture du fichier .pth
        pth_file = os.path.join(tmp_site, "cluster-ci-prefix.pth")
        with open(pth_file, "w") as f:
            for p in sorted(cands):
                f.write(p + "\n")

        with open(pth_file, "r") as f:
            lines = [l.strip() for l in f if l.strip()]
        assert lines == sorted([dist_pkg, site_pkg])

        # 3. Test de fail-fast : doit échouer si un chemin manque dans sys.path
        simulated_sys_path = ["", "/usr/lib/python3.12", site_pkg] # dist_pkg manque
        missing = [p for p in cands if p not in simulated_sys_path]
        assert missing == [dist_pkg]

        # 4. Test de fail-fast : doit réussir si tous les chemins sont présents
        simulated_sys_path.append(dist_pkg)
        missing_ok = [p for p in cands if p not in simulated_sys_path]
        assert missing_ok == []
