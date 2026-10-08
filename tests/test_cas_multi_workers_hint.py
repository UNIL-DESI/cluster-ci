"""
Tests unitaires ciblés pour le Correctif 1 : CAS multi-machines et court-circuit workers_hint (Point C).
Vérifie que :
1. Avec un workers_hint incomplet (ex: seul le worker local), le runner interroge
   systématiquement le headnode et découvre les autres pairs pour le CAS.
2. Si un pair remote est renvoyé par le headnode, il est fusionné dans sources_map
   aux côtés du worker local.
3. Si le headnode ne renvoie aucun pair et qu'aucun worker ne détient la dépendance,
   le résultat CAS est un échec explicite (sans fallback silencieux).
"""

from unittest.mock import MagicMock, patch
from src.runner.branch_executor import BranchExecutor


def test_cas_fetch_queries_headnode_even_when_workers_hint_provided(tmp_path):
    """
    Prouve qu'avec un workers_hint non-vide (ex. worker local),
    le runner interroge le headnode pour découvrir les pairs détenant
    la dépendance amont et fusionne les adresses dans sources_map.
    """
    executor = BranchExecutor(
        headnode_url="http://mock-headnode:5000",
        job_id="test-job-cas",
        runner_id="runner-1",
        worker_id="worker-local",
        repo_dir=str(tmp_path),
        target_repo="test-repo",
        target_branch="main",
        start_commit="HEAD",
    )

    expected_md5 = "e1b2c3d4e5f60718293a4b5c6d7e8f90"
    dep_path = "data/upstream_output.parquet"

    # Mock de get_dag_stage_outputs pour simuler une sortie de stage DVC absente localement
    with patch("src.runner.branch_executor.get_dag_stage_outputs") as mock_dag_outs, \
         patch.object(executor, "_get_workers_from_headnode") as mock_get_workers, \
         patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch_deps:

        mock_dag_outs.return_value = (
            {dep_path: {"stage": "stage_upstream", "cache": True, "md5": expected_md5}},
            {},
            []
        )
        # Le headnode connaît un autre worker pair détenant potentiellement l'artefact
        mock_get_workers.return_value = [
            {"worker_id": "worker-remote", "service_url": "http://worker-remote:6000"}
        ]
        mock_fetch_deps.return_value = MagicMock(
            success=True,
            status="success",
            missing_deps=[],
            missing_hashes=[],
            transfers=[]
        )

        # workers_hint ne contient que le worker local
        resources = {"workers": ["http://worker-local:6000"]}

        missing = executor.fetch_missing_deps(
            node="stage_downstream",
            dep_paths=[dep_path],
            resources=resources,
        )

        assert missing == []
        # Le headnode DOIT impérativement être interrogé malgré workers_hint non-vide
        assert mock_get_workers.called, "Le runner doit interroger le headnode pour découvrir tous les pairs actifs !"

        # Vérifier que les sources transmises à fetch_dependencies contiennent à la fois le local et le remote
        assert mock_fetch_deps.called
        call_kwargs = mock_fetch_deps.call_args[1]
        sources_map = call_kwargs["sources_map"]

        assert expected_md5 in sources_map
        candidate_sources = sources_map[expected_md5]
        assert "http://worker-local:6000" in candidate_sources
        assert "http://worker-remote:6000" in candidate_sources, (
            f"Le pair distant découvert via headnode doit être présent dans sources_map: {candidate_sources}"
        )


def test_cas_fetch_explicit_failure_when_no_peer_has_dependency(tmp_path):
    """
    Prouve qu'en l'absence de pair détenant la dépendance,
    fetch_missing_deps renvoie la dépendance manquante sans fallback silencieux.
    """
    executor = BranchExecutor(
        headnode_url="http://mock-headnode:5000",
        job_id="test-job-cas",
        runner_id="runner-1",
        worker_id="worker-local",
        repo_dir=str(tmp_path),
        target_repo="test-repo",
        target_branch="main",
        start_commit="HEAD",
    )

    expected_md5 = "deadbeefdeadbeefdeadbeefdeadbeef"
    dep_path = "data/missing_artifact.parquet"

    with patch("src.runner.branch_executor.get_dag_stage_outputs") as mock_dag_outs, \
         patch.object(executor, "_get_workers_from_headnode") as mock_get_workers, \
         patch("src.runner.branch_executor.fetch_dependencies") as mock_fetch_deps:

        mock_dag_outs.return_value = (
            {dep_path: {"stage": "stage_upstream", "cache": True, "md5": expected_md5}},
            {},
            []
        )
        mock_get_workers.return_value = []
        mock_fetch_deps.return_value = MagicMock(
            success=False,
            status="missing_deps",
            missing_deps=[dep_path],
            missing_hashes=[expected_md5],
            error_message="CAS object not found on any peer",
        )

        missing = executor.fetch_missing_deps(
            node="stage_downstream",
            dep_paths=[dep_path],
            resources={"workers": ["http://worker-local:6000"]},
        )

        assert dep_path in missing
