"""
Tests unitaires ciblés pour le Bug 6 : Transfert CAS P2P inter-workers.
Vérifie :
1. La normalisation robuste des URLs workers (noms nus, IP, ports par défaut 6000, protocoles).
2. La détection et transmission des artefacts cross-jobs via node_artifacts.
3. Les logs et métriques observables de transfert CAS (worker source, hash, taille, durée).
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src.runner.fetch_cas_dependencies import (
    build_candidate_urls,
    download_single_object,
    normalize_worker_url,
)
from src.scheduler.artifact_registry import ensure_schema, sources_for


def test_normalize_worker_url_variations():
    """Vérifie la normalisation sans faille de toutes les variantes d'adresses worker."""
    assert normalize_worker_url("HEC45801") == "http://HEC45801:6000"
    assert normalize_worker_url("HEC45801:6000") == "http://HEC45801:6000"
    assert normalize_worker_url("http://HEC45801") == "http://HEC45801:6000"
    assert normalize_worker_url("http://HEC45801:6000/") == "http://HEC45801:6000"
    assert normalize_worker_url("130.223.73.209") == "http://130.223.73.209:6000"
    assert normalize_worker_url("https://worker.domain.ch:8080") == "https://worker.domain.ch:8080"
    assert normalize_worker_url("https://worker.domain.ch") == "https://worker.domain.ch:6000"

    with pytest.raises(ValueError):
        normalize_worker_url("")


def test_build_candidate_urls_with_raw_hostname():
    """Vérifie que build_candidate_urls accepte un hostname nu sans lever InvalidURL."""
    md5 = "abcdef0123456789abcdef0123456789"
    urls = build_candidate_urls("HEC45801", md5, repo_name="my_repo")
    assert any("http://HEC45801:6000" in u for u in urls)
    for u in urls:
        assert u.startswith("http://")


def test_download_single_object_observable_metrics(tmp_path):
    """Vérifie que download_single_object mesure la taille et durée et retourne un DownloadResult enrichi."""
    md5 = "e1b2c3d4e5f60718293a4b5c6d7e8f90"
    content = b"TEST_CAS_CONTENT_BYTES"
    cache_dir = tmp_path / "cache"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_content.return_value = [content]

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with patch("hashlib.md5") as mock_hasher:
        hasher_inst = MagicMock()
        hasher_inst.hexdigest.return_value = md5
        mock_hasher.return_value = hasher_inst

        res = download_single_object(
            md5_hash=md5,
            candidate_sources=["HEC45801"],
            cache_dir=cache_dir,
            session=mock_session,
        )

    assert isinstance(res, tuple)
    assert res[0] is True
    assert res[1] == "downloaded"
    assert hasattr(res, "transfer_info")
    info = res.transfer_info
    assert info is not None
    assert info["hash"] == md5
    assert info["size_bytes"] == len(content)
    assert info["duration_s"] >= 0.0
    assert "HEC45801" in info["source"]


def test_cross_job_artifact_resolution(tmp_path):
    """Vérifie que les artefacts produits par un run antérieur (cross-jobs) sont résolus pour un nouveau job."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    conn.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY, is_local INTEGER)")
    conn.execute("INSERT INTO jobs VALUES ('job-prev', 0)")

    cursor = conn.cursor()
    # Job antérieur job-prev produit un artefact sur HEC45801
    cursor.execute("""
        INSERT INTO node_artifacts (job_id, node_name, md5, is_dir, size_bytes, worker_id, path)
        VALUES ('job-prev', 'step1', 'hash12345', 0, 1024, 'worker-1', 'data/features.parquet')
    """)
    conn.commit()

    online_workers = {"worker-1": "http://HEC45801:6000"}
    dep_paths_list = ["data/features.parquet"]
    current_job_id = "job-current"

    # Simulation du code scheduler_loop pour trouver dep_hashes
    placeholders = ','.join(['?'] * len(dep_paths_list))
    cursor.execute(f"SELECT DISTINCT md5 FROM node_artifacts WHERE job_id = ? AND path IN ({placeholders})", [current_job_id, *dep_paths_list])
    dep_hashes = [r[0] for r in cursor.fetchall() if r[0]]
    assert len(dep_hashes) == 0  # Absent du job courant

    # Recherche cross-jobs
    cursor.execute(f"SELECT DISTINCT md5 FROM node_artifacts WHERE path IN ({placeholders}) ORDER BY created_at DESC", dep_paths_list)
    for r in cursor.fetchall():
        if r[0] and r[0] not in dep_hashes:
            dep_hashes.append(r[0])

    assert dep_hashes == ["hash12345"]
    sources = sources_for(conn, dep_hashes, online_workers)
    assert "hash12345" in sources
    assert sources["hash12345"] == ["http://HEC45801:6000"]
    conn.close()
