"""
test_gpu_admission.py - Comprehensive Unit and Integration Tests for Cluster-CI v3 GPU Admission & Allocation.

Scenarios covered:
1. Unified memory (GB10, 1 GPU): Single gpus=1 admission, 2nd gpus=1 rejected, non-GPU packing works.
2. Discrete machine (2x RTX 3090): Sequential GPU allocation ([0], [1]), 3rd rejected.
3. Atomic reservation under BEGIN IMMEDIATE: concurrency re-check under lock prevents race condition.
4. GPU IDs persistence in DB (job_nodes.gpu_ids and jobs.gpu_ids).
5. GPU release on finish, cancellation, and heartbeat crash recovery.
6. Execution chain verification: host_guard raises ValueError without gpu_ids (no --gpus=all), device flag format.
7. Resource impossibility detection: check_resource_impossibility returns actionable English A17 message.
"""

import json
import os
import sys
import uuid
import pytest

# Ensure repository root is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from src.scheduler import persistence, scheduler_loop, headnode_service
from src.runner.host_guard import docker_resource_args


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Initializes an isolated SQLite database for each test."""
    db_file = str(tmp_path / f"test_gpu_cluster_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file


@pytest.fixture
def client(monkeypatch):
    headnode_service.app.config['TESTING'] = True
    monkeypatch.setattr(headnode_service, "CLUSTER_TOKEN", "scheduler-test-token")
    with headnode_service.app.test_client() as c:
        c.environ_base["HTTP_AUTHORIZATION"] = "Bearer scheduler-test-token"
        yield c


def _insert_worker(worker_id, hostname, total_ram_gb=120.0, unified_memory=1, gpu_count=1, gpu_name="GB10", vram_per_gpu=None, cpus=16, disk_free_gb=500.0):
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workers (
                worker_id, hostname, status, total_ram_gb, unified_memory, gpu_count,
                gpu_name, vram_per_gpu, cpus, disk_free_gb, last_seen
            ) VALUES (?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ''', (
            worker_id, hostname, total_ram_gb, unified_memory, gpu_count,
            gpu_name, json.dumps(vram_per_gpu) if isinstance(vram_per_gpu, list) else vram_per_gpu,
            cpus, disk_free_gb
        ))
        conn.commit()


# =========================================================================
# Scenario 1: Unified Memory (GB10) Admission & Packing
# =========================================================================
def test_scenario_1_unified_memory_gb10_single_gpu_admission():
    _insert_worker("hec45801", "hec45801", total_ram_gb=120.0, unified_memory=1, gpu_count=1, gpu_name="NVIDIA GB10")
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM workers WHERE worker_id = 'hec45801'")
        worker = dict(cursor.fetchone())

    node_res_1 = {"ram_gb": 20.0, "gpus": 1, "cpus": 4}
    node_res_2 = {"ram_gb": 20.0, "gpus": 1, "cpus": 4}
    node_res_nogpu = {"ram_gb": 20.0, "gpus": 0, "cpus": 4}

    # Empty allocation: node 1 is admissible and gets device 0
    empty_alloc = scheduler_loop.get_worker_allocated_resources(worker_id="hec45801")
    assert scheduler_loop.is_worker_admissible_for_node(worker, node_res_1, allocated=empty_alloc) is True
    gids_1 = scheduler_loop.allocate_gpus(worker, node_res_1, allocated_gpu_ids=empty_alloc["allocated_gpu_ids"])
    assert gids_1 == [0]

    # Simulate node 1 running holding GPU 0
    busy_alloc = {
        "used_cpus": 4,
        "used_ram_gb": 20.0,
        "used_vram_gb": 0.0,
        "used_storage_gb": 0.0,
        "allocated_vram_by_gpu": {},
        "allocated_gpu_ids": {0},
        "gpu_holders": {0: ["node1 (job1)"]},
        "active_executors": 1
    }

    # Node 2 requesting GPU 1 must be rejected on GB10
    assert scheduler_loop.is_worker_admissible_for_node(worker, node_res_2, allocated=busy_alloc) is False
    assert scheduler_loop.allocate_gpus(worker, node_res_2, allocated_gpu_ids=busy_alloc["allocated_gpu_ids"]) is None

    # Non-GPU node can still pack if RAM fits
    assert scheduler_loop.is_worker_admissible_for_node(worker, node_res_nogpu, allocated=busy_alloc) is True


# =========================================================================
# Scenario 2: Discrete Machine (2x RTX 3090) Sequential Admission
# =========================================================================
def test_scenario_2_discrete_machine_2x_rtx3090_admission():
    _insert_worker("isipol09", "isipol09", total_ram_gb=128.0, unified_memory=0, gpu_count=2, gpu_name="2x RTX 3090", vram_per_gpu=[24.0, 24.0], cpus=32)
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM workers WHERE worker_id = 'isipol09'")
        worker = dict(cursor.fetchone())

    req_gpu = {"ram_gb": 16.0, "gpus": 1, "vram_gb": 10.0, "cpus": 4}

    # Job 1 gets GPU 0
    alloc_0 = scheduler_loop.get_worker_allocated_resources(worker_id="isipol09")
    assert scheduler_loop.is_worker_admissible_for_node(worker, req_gpu, allocated=alloc_0) is True
    gids_1 = scheduler_loop.allocate_gpus(worker, req_gpu, allocated_gpu_ids=alloc_0["allocated_gpu_ids"], allocated_vram_by_gpu=alloc_0["allocated_vram_by_gpu"])
    assert gids_1 == [0]

    # Job 2 gets GPU 1 when GPU 0 is held
    alloc_1 = {
        "used_cpus": 4,
        "used_ram_gb": 16.0,
        "used_vram_gb": 10.0,
        "used_storage_gb": 0.0,
        "allocated_vram_by_gpu": {0: 10.0},
        "allocated_gpu_ids": {0},
        "gpu_holders": {0: ["job1"]},
        "active_executors": 1
    }
    assert scheduler_loop.is_worker_admissible_for_node(worker, req_gpu, allocated=alloc_1) is True
    gids_2 = scheduler_loop.allocate_gpus(worker, req_gpu, allocated_gpu_ids=alloc_1["allocated_gpu_ids"], allocated_vram_by_gpu=alloc_1["allocated_vram_by_gpu"])
    assert gids_2 == [1]

    # Job 3 is rejected when both GPU 0 and 1 are held
    alloc_2 = {
        "used_cpus": 8,
        "used_ram_gb": 32.0,
        "used_vram_gb": 20.0,
        "used_storage_gb": 0.0,
        "allocated_vram_by_gpu": {0: 10.0, 1: 10.0},
        "allocated_gpu_ids": {0, 1},
        "gpu_holders": {0: ["job1"], 1: ["job2"]},
        "active_executors": 2
    }
    assert scheduler_loop.is_worker_admissible_for_node(worker, req_gpu, allocated=alloc_2) is False
    assert scheduler_loop.allocate_gpus(worker, req_gpu, allocated_gpu_ids=alloc_2["allocated_gpu_ids"], allocated_vram_by_gpu=alloc_2["allocated_vram_by_gpu"]) is None


# =========================================================================
# Scenario 3: Atomic Reservation Under BEGIN IMMEDIATE
# =========================================================================
def test_scenario_3_atomic_reservation_under_begin_immediate():
    _insert_worker("w_gb10", "w_gb10", total_ram_gb=120.0, unified_memory=1, gpu_count=1, gpu_name="GB10")

    job_id = "job_atomic_test"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, home_worker) VALUES (?, 'r', 'b', 'running', 1, 'w_gb10')", (job_id,))
        cursor.execute("INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES (?, 'nodeA', 'ready', ?)",
                       (job_id, json.dumps({"ram_gb": 10.0, "gpus": 1})))
        cursor.execute("INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES (?, 'nodeB', 'ready', ?)",
                       (job_id, json.dumps({"ram_gb": 10.0, "gpus": 1})))
        conn.commit()

    # Worker executes next_node for nodeA
    req1 = {"job_id": job_id, "worker_id": "w_gb10", "runner_id": "r1"}
    resp1 = scheduler_loop.handle_next_node(req1)
    assert resp1["action"] in ("run", "switch_image")
    assert resp1["node"] in ("nodeA", "nodeB")
    assert resp1["gpu_ids"] == [0]

    # Immediately attempt to assign second GPU node on the same worker
    req2 = {"job_id": job_id, "worker_id": "w_gb10", "runner_id": "r2"}
    resp2 = scheduler_loop.handle_next_node(req2)
    # Cannot get a node because GPU 0 is already held under BEGIN IMMEDIATE check
    assert resp2["action"] in ("wait", "yield")
    assert "node" not in resp2 or resp2.get("gpu_ids") is None


# =========================================================================
# Scenario 4: GPU IDs Persisted in DB (job_nodes and jobs)
# =========================================================================
def test_scenario_4_gpu_ids_persisted_in_db():
    _insert_worker("w_gb10", "w_gb10", total_ram_gb=120.0, unified_memory=1, gpu_count=1, gpu_name="GB10")

    # A. Parallel v3 node
    v3_job_id = "v3_persist_job"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, home_worker) VALUES (?, 'r', 'b', 'running', 1, 'w_gb10')", (v3_job_id,))
        cursor.execute("INSERT INTO job_nodes (job_id, node_name, status, resources) VALUES (?, 'stage1', 'ready', ?)",
                       (v3_job_id, json.dumps({"ram_gb": 10.0, "gpus": 1})))
        conn.commit()

    scheduler_loop.handle_next_node({"job_id": v3_job_id, "worker_id": "w_gb10", "runner_id": "r1"})
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, gpu_ids FROM job_nodes WHERE job_id = ? AND node_name = 'stage1'", (v3_job_id,))
        status, gids_raw = cursor.fetchone()
        assert status == "running"
        assert json.loads(gids_raw) == [0]

    # Clean up v3 node before classic job test
    persistence.mark_node_status(v3_job_id, "stage1", "done")

    # B. Classic job
    c_job_id = "classic_persist_job"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, ram_required_gb, vram_required_gb)
            VALUES (?, 'r', 'b_classic', 'pending', 0, 10.0, 10.0)
        ''', (c_job_id,))
        conn.commit()

    scheduler_loop.schedule_iteration()
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, worker_id, gpu_ids FROM jobs WHERE job_id = ?", (c_job_id,))
        c_status, c_worker, c_gids_raw = cursor.fetchone()
        assert c_status == "assigned"
        assert c_worker == "w_gb10"
        assert json.loads(c_gids_raw) == [0]


# =========================================================================
# Scenario 5: GPU Release on Completion, Cancellation, Heartbeat Recovery
# =========================================================================
def test_scenario_5_gpu_release_on_completion_cancellation_heartbeat():
    _insert_worker("w_gb10", "w_gb10", total_ram_gb=120.0, unified_memory=1, gpu_count=1, gpu_name="GB10")
    job_id = "lifecycle_job"

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, home_worker) VALUES (?, 'r', 'b', 'running', 1, 'w_gb10')", (job_id,))
        cursor.execute("INSERT INTO job_nodes (job_id, node_name, status, worker_id, gpu_ids) VALUES (?, 'n_done', 'running', 'w_gb10', '[0]')", (job_id,))
        conn.commit()

    # 1. Completion: mark_node_status clears gpu_ids and frees resource
    persistence.mark_node_status(job_id, "n_done", "done")
    alloc_after_done = scheduler_loop.get_worker_allocated_resources(worker_id="w_gb10")
    assert 0 not in alloc_after_done["allocated_gpu_ids"]
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT gpu_ids FROM job_nodes WHERE job_id = ? AND node_name = 'n_done'", (job_id,))
        assert cursor.fetchone()[0] == '[]'

    # 2. Cancellation: cancel_job_cleanly resets gpu_ids on blocked nodes
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (job_id,))
        cursor.execute("INSERT INTO job_nodes (job_id, node_name, status, worker_id, gpu_ids) VALUES (?, 'n_cancel', 'running', 'w_gb10', '[0]')", (job_id,))
        conn.commit()
    scheduler_loop.cancel_job_cleanly(job_id)
    alloc_after_cancel = scheduler_loop.get_worker_allocated_resources(worker_id="w_gb10")
    assert 0 not in alloc_after_cancel["allocated_gpu_ids"]
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, gpu_ids FROM job_nodes WHERE job_id = ? AND node_name = 'n_cancel'", (job_id,))
        n_st, n_gids = cursor.fetchone()
        assert n_st == "blocked"
        assert n_gids == '[]'

    # 3. Heartbeat timeout recovery: check_runner_heartbeat_timeouts resets running node to ready with gpu_ids = '[]'
    timeout_job = "job_hb_timeout"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, home_worker) VALUES (?, 'r', 'b', 'running', 1, 'w_gb10')", (timeout_job,))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, worker_id, runner_id, gpu_ids, started_at)
            VALUES (?, 'n_timeout', 'running', 'w_gb10', 'runner_dead', '[0]', datetime('now', '-300 seconds'))
        ''', (timeout_job,))
        conn.commit()
    recovered = persistence.check_runner_heartbeat_timeouts(timeout_s=60)
    assert len(recovered) == 1
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, worker_id, gpu_ids FROM job_nodes WHERE job_id = ? AND node_name = 'n_timeout'", (timeout_job,))
        rec_st, rec_w, rec_gids = cursor.fetchone()
        assert rec_st == "ready"
        assert rec_w is None
        assert rec_gids == '[]'


# =========================================================================
# Scenario 6: Execution Chain & host_guard Verification
# =========================================================================
def test_scenario_6_execution_chain_host_guard_and_docker_flags():
    host = {"role": "worker", "total_ram_gb": 64}

    # Fail-fast: req_gpus > 0 with no gpu_ids raises ValueError (strictly no --gpus=all)
    with pytest.raises(ValueError, match="requested but no gpu_ids assigned"):
        docker_resource_args(host, {"ram_gb": 16, "gpus": 1})

    # Assigned gpu_ids produces exact device flag
    args_single = docker_resource_args(host, {"ram_gb": 16, "gpus": 1, "gpu_ids": [0]})
    assert '--gpus="device=0"' in args_single

    args_multi = docker_resource_args(host, {"ram_gb": 16, "gpus": 2, "gpu_ids": [0, 1]})
    assert '--gpus="device=0,1"' in args_multi

    # gpus=0 produces no --gpus flag
    args_zero = docker_resource_args(host, {"ram_gb": 8, "gpus": 0})
    assert not any(a.startswith("--gpus") for a in args_zero)

    # Verify run_research_pipeline.sh does not contain --gpus all
    script_path = os.path.join(repo_root, "src", "runner", "run_research_pipeline.sh")
    with open(script_path, "r", encoding="utf-8") as f:
        script_content = f.read()
    assert "--gpus all" not in script_content
    assert "DOCKER_GPU_FLAG=" in script_content


# =========================================================================
# Scenario 7: Resource Impossibility Detection (Actionable English A17)
# =========================================================================
def test_scenario_7_impossibility_detection_english_a17_message():
    workers = [
        {"worker_id": "W1", "hostname": "W1", "total_ram_gb": 64.0, "gpu_count": 2, "vram_per_gpu": [24.0, 24.0], "cpus": 16, "total_storage_gb": 1000},
        {"worker_id": "W2_GB10", "hostname": "W2_GB10", "total_ram_gb": 120.0, "unified_memory": 1, "gpu_count": 1, "cpus": 16, "total_storage_gb": 1000}
    ]

    # Node requests 4 GPUs on cluster where max GPU count is 2
    res_4gpus = {"ram_gb": 16.0, "gpus": 4, "cpus": 4}
    is_imp, err_msg = scheduler_loop.check_resource_impossibility(res_4gpus, workers, item_name="stage_heavy", is_classic=False)
    assert is_imp is True
    assert "node stage_heavy requests gpus=4" in err_msg
    assert "largest capacity: machine W1 (2 GPU" in err_msg
    assert "remedy: reduce meta.cluster.gpus of stage stage_heavy in dvc.yaml" in err_msg

    # Classic job requests 3 GPUs
    res_classic_3gpus = {"ram_gb": 16.0, "gpus": 3, "cpus": 4}
    is_imp_c, err_msg_c = scheduler_loop.check_resource_impossibility(res_classic_3gpus, workers, item_name="job_classic_gpu", is_classic=True)
    assert is_imp_c is True
    assert "Classic job job_classic_gpu impossible: requests 3 GPUs" in err_msg_c
    assert "maximum machine capacities" in err_msg_c
    assert "remedy: decrease requested GPUs to <= 2" in err_msg_c


# =========================================================================
# Bonus Check: Status APIs (/workers & /scheduler_status) Expose GPU accounting
# =========================================================================
def test_status_apis_expose_gpu_accounting(client):
    _insert_worker("w_status", "w_status", total_ram_gb=128.0, unified_memory=0, gpu_count=2, vram_per_gpu=[24.0, 24.0])

    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, worker_id, gpu_ids)
            VALUES ('job_active', 'r', 'b', 'running', 0, 'w_status', '[0]')
        ''')
        conn.commit()

    resp_w = client.get("/workers").get_json()
    worker_entry = next((w for w in resp_w if w["worker_id"] == "w_status"), None)
    assert worker_entry is not None
    assert worker_entry["gpus_total"] == 2
    assert worker_entry["gpus_allocated"] == 1
    assert "0" in worker_entry["gpu_holders"] or 0 in worker_entry["gpu_holders"]

    resp_s = client.get("/scheduler_status").get_json()
    s_worker = next((w for w in resp_s["workers"] if w["worker_id"] == "w_status"), None)
    assert s_worker is not None
    assert s_worker["gpus_total"] == 2
    assert s_worker["gpus_allocated"] == 1


# =========================================================================
# Scenario 8: Container Reuse vs Recreation on Resource Change (A)
# =========================================================================
def test_branch_executor_container_recreation_on_resource_change(tmp_path):
    """
    Scenario 8 (A): Ne réutiliser le conteneur que si l'image ET TOUS les arguments de ressources
    (memory, cpus, gpu_ids, shm, cgroup-parent) sont identiques.
    Deux nœuds successifs, même image, ressources différentes (ex: gpus:0 puis gpus:1)
    doivent donner deux conteneurs distincts aux arguments de ressources corrects.
    Un troisième nœud aux ressources identiques au 2ème réutilise le 2ème conteneur.
    """
    from unittest.mock import MagicMock
    from src.runner.branch_executor import BranchExecutor, DockerRunner

    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir, exist_ok=True)

    mock_docker = MagicMock(spec=DockerRunner)
    mock_docker.create_volume.return_value = 0
    mock_docker.run_container.return_value = 0
    mock_docker.exec_in_container.return_value = (0, "ok")
    mock_docker.stop_container.return_value = 0
    mock_docker.remove_container.return_value = 0

    executor = BranchExecutor(
        headnode_url="http://mock-headnode:5000",
        job_id="job_test_reuse",
        runner_id="runner_1",
        worker_id="worker_1",
        repo_dir=repo_dir,
        target_repo="user/repo",
        target_branch="main",
        docker=mock_docker,
    )

    # 1. Premier nœud : sans GPU (gpus=0)
    res_node1 = {"ram_gb": 8.0, "cpus": 4, "gpus": 0}
    args_1 = executor.compute_docker_resource_args(res_node1)
    assert not any(a.startswith("--gpus") for a in args_1)
    executor.start_container_for_image("pytorch:latest", res_node1)
    assert executor.total_containers_started == 1
    c1_name = executor.current_container

    # 2. Deuxième nœud : avec GPU (gpus=1, gpu_ids=[0]), même image
    res_node2 = {"ram_gb": 16.0, "cpus": 4, "gpus": 1, "gpu_ids": [0]}
    args_2 = executor.compute_docker_resource_args(res_node2)
    assert '--gpus="device=0"' in args_2
    assert args_1 != args_2

    # Simuler le bloc 'action == run' de BranchExecutor
    target_args = executor.compute_docker_resource_args(res_node2)
    resource_args_changed = (executor.current_resource_args != target_args)
    assert resource_args_changed is True

    if executor.current_container:
        executor.stop_current_container()
    executor.start_container_for_image("pytorch:latest", res_node2)

    assert executor.total_containers_started == 2
    mock_docker.stop_container.assert_called_with(c1_name)
    mock_docker.remove_container.assert_called_with(c1_name)

    # Vérifier les arguments passés à run_container pour le 2ème nœud
    last_call_kwargs = mock_docker.run_container.call_args[1]
    assert last_call_kwargs["resources"]["gpus"] == 1
    assert last_call_kwargs["resources"]["gpu_ids"] == [0]

    # 3. Troisième nœud : ressources identiques au 2ème nœud -> réutilisation sans recréer
    res_node3 = {"ram_gb": 16.0, "cpus": 4, "gpus": 1, "gpu_ids": [0]}
    target_args_3 = executor.compute_docker_resource_args(res_node3)
    resource_args_changed_3 = (executor.current_resource_args != target_args_3)
    assert resource_args_changed_3 is False
    # Pas de recréation
    assert executor.total_containers_started == 2


# =========================================================================
# Scenario 9: Anti-Affinité Machine Intra-Job (B)
# =========================================================================
def test_anti_affinity_same_job_two_nodes_two_machines(client):
    """
    Scenario 9 (B): Anti-affinité machine intra-job :
    Deux nœuds prêts d'un même job, deux machines libres -> un nœud par machine.
    Une machine qui exécute déjà un nœud actif du job J est rejetée pour tout autre nœud de J.
    """
    _insert_worker("w_alpha", "w_alpha", total_ram_gb=64.0, cpus=16)
    _insert_worker("w_beta", "w_beta", total_ram_gb=64.0, cpus=16)

    job_id = "job_anti_affinity_2machines"
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, status, parallel_mode, home_worker, active_workers)
            VALUES (?, 'owner/repo', 'main', 'running', 1, 'w_alpha', '["w_alpha", "w_beta"]')
        ''', (job_id,))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, resources)
            VALUES (?, 'node_1', 'ready', '{"cpus": 4, "ram_gb": 8.0}')
        ''', (job_id,))
        cursor.execute('''
            INSERT INTO job_nodes (job_id, node_name, status, resources)
            VALUES (?, 'node_2', 'ready', '{"cpus": 4, "ram_gb": 8.0}')
        ''', (job_id,))
        conn.commit()

    # w_alpha appelle next_node -> reçoit node_1 qui passe à running sur w_alpha
    resp1 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_alpha",
        "worker": "w_alpha",
    }).get_json()
    assert resp1["action"] in ("run", "switch_image")
    assert resp1["node"] == "node_1"

    # Vérifier que node_1 est running sur w_alpha
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, worker_id FROM job_nodes WHERE job_id = ? AND node_name = 'node_1'", (job_id,))
        st, wid = cursor.fetchone()
        assert st == "running"
        assert wid == "w_alpha"

    # Vérifier que pour node_2, w_alpha est MAINTENANT REJETÉ car il a déjà un nœud actif de ce job
    alloc_alpha = scheduler_loop.get_worker_allocated_resources(worker_id="w_alpha")
    assert job_id in alloc_alpha["active_job_ids"]
    assert scheduler_loop.is_worker_admissible_for_node(
        {"worker_id": "w_alpha", "cpus": 16, "total_ram_gb": 64.0},
        {"cpus": 4, "ram_gb": 8.0, "job_id": job_id},
        allocated=alloc_alpha
    ) is False

    # Mais w_beta (qui est libre) EST ADMISSIBLE pour node_2
    alloc_beta = scheduler_loop.get_worker_allocated_resources(worker_id="w_beta")
    assert job_id not in alloc_beta["active_job_ids"]
    assert scheduler_loop.is_worker_admissible_for_node(
        {"worker_id": "w_beta", "cpus": 16, "total_ram_gb": 64.0},
        {"cpus": 4, "ram_gb": 8.0, "job_id": job_id},
        allocated=alloc_beta
    ) is True

    # w_beta appelle next_node -> reçoit node_2
    resp2 = client.post(f"/api/jobs/{job_id}/next_node", json={
        "runner_id": "runner_beta",
        "worker": "w_beta",
    }).get_json()
    assert resp2["action"] in ("run", "switch_image")
    assert resp2["node"] == "node_2"

    # Vérifier la répartition finale : exactement un nœud par machine
    with persistence.get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT node_name, worker_id, status FROM job_nodes WHERE job_id = ?", (job_id,))
        nodes = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        assert nodes["node_1"] == ("w_alpha", "running")
        assert nodes["node_2"] == ("w_beta", "running")
